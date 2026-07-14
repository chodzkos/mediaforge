"""Budowa komend FFmpeg dla nagrywania ekranu/audio — czysta logika (bez subprocess).

Wydzielone od orkiestracji (:mod:`.recorder`), żeby budowanie komend było w pełni
testowalne bez uruchamiania FFmpeg ani bycia na Windows. Funkcje są deterministyczne:
te same wejścia → ta sama lista argumentów.

Cel platformowy to Windows (nagrywanie): wideo przez ``ddagrab`` (Desktop Duplication
API, D3D11, GPU — jak OBS; ``gdigrab``/GDI gubił klatki przy pełnoekranowym wideo bo
jest CPU-bound), audio przez ``dshow`` (urządzenie WASAPI loopback dla dźwięku
systemowego oraz mikrofon). Wybór monitora = ``output_idx`` ddagrab (hook pod hybrydę
iGPU+dGPU); pod-region realizuje ``crop`` w filtrze (względem monitora). Okno po tytule
niewspierane przez ddagrab — tryb usunięty (nie udajemy cap/funkcji-widma).

Wybór enkodera pod capture NA ŻYWO: :func:`select_video_encoder` schodzi po łańcuchu
preferencji do pierwszego enkodera dostępnego w buildzie FFmpeg
(``check_ffmpeg()['encoders']`` / ``encoders_usable``). Łańcuchy WYCZERPUJĄ tor sprzętowy
(NVENC → AMF → QSV) PRZED zejściem na software, bo dla nagrywania liczy się „zdąży zakodować",
nie „preferowany kodek". Ostateczny software-fallback = wyłącznie ``libx264 -preset veryfast``;
software-HEVC (``libx265``) jest z realtime wykluczony (za wolny na CPU przy 60 fps). Przy
software-torze wymuszamy fps ≤ 30 i skalę ≤ 1920 px (bez tego fallback = szarpanie innym kodekiem).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from mediaforge.core.engines.base import QualityOption


class CaptureMode(StrEnum):
    """Tryb źródła wideo (ddagrab łapie monitor; okno po tytule niewspierane)."""

    FULLSCREEN = "fullscreen"  # cały wybrany monitor (region=None)
    REGION = "region"  # crop prostokąta x,y,w,h WEWNĄTRZ wybranego monitora


@dataclass(slots=True)
class CaptureSource:
    """Co nagrywamy: ``monitor`` = ``output_idx`` ddagrab, opcjonalny ``region`` = crop.

    ``region`` (x, y, w, h, piksele fizyczne) jest WZGLĘDEM wybranego monitora (nie
    wirtualnego pulpitu) — ddagrab łapie ten monitor, crop tnie wewnątrz niego. ``None``
    = cały monitor. Walidację zakresu (mieści się w rozdzielczości) robi GUI, nie builder.
    """

    mode: CaptureMode = CaptureMode.FULLSCREEN
    monitor: int = 0
    region: tuple[int, int, int, int] | None = None  # x, y, w, h względem monitora


@dataclass(slots=True)
class AudioConfig:
    """Konfiguracja audio: dźwięk systemowy (WASAPI loopback) i/lub mikrofon, miks.

    Nazwy urządzeń ``dshow`` enumeruje GUI; ``system_device`` to urządzenie loopback.
    ``mix=True`` zlewa oba źródła w jeden ślad (``amix``); inaczej zostają osobne ślady.
    """

    system_audio: bool = True
    microphone: bool = False
    system_device: str | None = None
    mic_device: str | None = None
    mix: bool = False
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 160


# Presety jakości z ROADMAP/ARCHITECTURE (Ekonomiczny/Standard/Wysoka/Archiwum/Tylko audio).
# resolution=None → natywna rozdzielczość źródła; bitrate orientacyjny (target -b:v).
PRESETS: dict[str, QualityOption] = {
    "economy": QualityOption(
        label="Ekonomiczny",
        video_codec="hevc",
        audio_codec="aac",
        fps=24,
        bitrate_kbps=2500,
    ),
    "standard": QualityOption(
        label="Standard",
        video_codec="hevc",
        audio_codec="aac",
        fps=30,
        bitrate_kbps=6000,
    ),
    "high": QualityOption(
        label="Wysoka",
        video_codec="hevc",
        audio_codec="aac",
        fps=60,
        bitrate_kbps=12000,
    ),
    "archive": QualityOption(
        label="Archiwum",
        video_codec="av1",
        audio_codec="aac",
        fps=60,
        bitrate_kbps=40000,
    ),
    "audio_only": QualityOption(
        label="Tylko audio",
        video_codec=None,
        audio_codec="aac",
        bitrate_kbps=0,
        audio_only=True,
    ),
}

# Domyślny czas pojedynczego segmentu (s). Krótszy = mniejsza strata przy crashu.
DEFAULT_SEGMENT_SECONDS = 300

# Łańcuchy preferencji enkoderów: (nazwa, czy_sprzętowy). Cały tor SPRZĘTOWY (NVENC → AMF → QSV)
# WYCZERPANY zanim spadniemy na software — do capture na żywo liczy się „zdąży zakodować", nie
# „preferowany kodek". Ostateczny software-fallback to TYLKO libx264 (veryfast); libx265 i
# software-AV1 świadomie POZA realtime (za wolne na CPU przy 60 fps — zob. ROADMAP: sprzęt).
_CODEC_CHAINS: dict[str, list[tuple[str, bool]]] = {
    "hevc": [
        ("hevc_nvenc", True),
        ("h264_nvenc", True),
        ("hevc_amf", True),
        ("h264_amf", True),
        ("h264_qsv", True),
        ("libx264", False),
    ],
    "h264": [
        ("h264_nvenc", True),
        ("hevc_nvenc", True),
        ("h264_amf", True),
        ("hevc_amf", True),
        ("h264_qsv", True),
        ("libx264", False),
    ],
    "av1": [
        ("av1_nvenc", True),
        ("hevc_nvenc", True),
        ("h264_nvenc", True),
        ("hevc_amf", True),
        ("h264_amf", True),
        ("h264_qsv", True),
        ("libx264", False),
    ],
}

# Enkodery software WYKLUCZONE z wyboru do nagrywania na żywo (za wolne na CPU przy capture
# 60 fps). libx264 zostaje jedynym software-torem (przez łańcuch). libx265/software-AV1 mogłyby
# wrócić tylko dla przyszłego trybu re-enkodowania OFFLINE, nie dla realtime.
_NON_REALTIME_ENCODERS = frozenset({"libx265", "libsvtav1", "libaom-av1"})


@dataclass(slots=True)
class EncoderChoice:
    """Rozstrzygnięty enkoder wideo + czy jest sprzętowy (NVENC)."""

    name: str
    hardware: bool


def select_video_encoder(preferred_codec: str, encoders: Mapping[str, bool]) -> EncoderChoice:
    """Wybiera enkoder wideo wg preferencji kodeka, z fallbackiem programowym.

    Args:
        preferred_codec: ``"hevc"`` / ``"av1"`` / ``"h264"`` — żądana rodzina kodeka.
        encoders: mapa „nazwa enkodera → dostępny" (z ``check_ffmpeg()['encoders']``).

    Returns:
        :class:`EncoderChoice` — pierwszy dostępny enkoder z łańcucha; gdy nic z łańcucha
        nie jest obecne, dowolny dostępny, a w ostateczności ``libx264`` (nominalnie).
    """
    chain = _CODEC_CHAINS.get(preferred_codec, _CODEC_CHAINS["h264"])
    for name, hardware in chain:
        if encoders.get(name):
            return EncoderChoice(name, hardware)
    # Łańcuch pusty w tym buildzie — weź cokolwiek dostępnego (deterministycznie), ale NIGDY
    # enkodera software wykluczonego z realtime (libx265 itp.: za wolny na capture 60 fps).
    for name, available in encoders.items():
        if available and name not in _NON_REALTIME_ENCODERS:
            return EncoderChoice(name, _is_hardware(name))
    return EncoderChoice("libx264", False)


def _is_hardware(name: str) -> bool:
    """Czy enkoder jest sprzętowy (NVENC/AMF/QSV) — po sufiksie nazwy."""
    return name.endswith(("nvenc", "amf", "qsv"))


def encoder_quality_args(name: str) -> list[str]:
    """Flagi jakości/szybkości SPECYFICZNE dla rodziny enkodera (dobór po sufiksie nazwy).

    Presety ``p1``-``p7`` istnieją TYLKO w NVENC. Wpisanie ich obok innego enkodera wywala
    ffmpeg jeszcze przed startem (zmierzone: fallback na ``h264_qsv`` → „Unable to parse preset
    option value p5" → proces nie rusza). Każda rodzina ma własny słownik flag:

    * ``*_nvenc`` → ``-preset p5 -tune hq`` (sprzęt realtime-zdolny, jakość),
    * ``*_qsv``  → ``-preset veryfast`` (QSV używa nazw jak x264; NIE zna ``-tune``),
    * ``*_amf``  → ``-quality balanced`` (AMF nie ma ``-preset``; ma ``-quality``/``-usage``),
    * ``libx264`` → ``-preset veryfast -crf 23`` (software realtime).

    Nieznana rodzina → brak flag jakości (domyślne enkodera; nie ryzykujemy złej flagi).
    """
    if name.endswith("nvenc"):
        return ["-preset", "p5", "-tune", "hq"]
    if name.endswith("qsv"):
        return ["-preset", "veryfast"]
    if name.endswith("amf"):
        return ["-quality", "balanced"]
    if name == "libx264":
        return ["-preset", "veryfast", "-crf", "23"]
    return []


def record_framerate(preset_fps: int | None, *, software: bool) -> int:
    """FPS nagrania z presetu, z adaptacją pod tor software.

    Sprzęt: min. 60 fps (30 dawało szarpany steady-state, sprzęt wyrabia). Software (libx264):
    ≤ 30 fps — 60 fps na słabym CPU to ciągłe dropy, więc obniżka jest WARUNKIEM używalności
    software-toru, nie opcją (nawet gdy preset/config prosi o 60).
    """
    if software:
        return min(preset_fps or 30, 30)
    return max(preset_fps or 60, 60)


def _even(n: int) -> int:
    """Zaokrągla w dół do parzystej (yuv420p wymaga parzystych w/h)."""
    return n - (n % 2)


def build_video_filter(
    region: tuple[int, int, int, int] | None, *, max_width: int | None = None
) -> str:
    """Wartość -vf. region = (x, y, w, h) względem WYBRANEGO monitora (None = cały monitor).

    ``hwdownload,format=bgra`` ściąga tekstury ddagrab z GPU do RAM; ``crop`` wycina prostokąt
    PO pobraniu; ``yuv420p`` wymaga PARZYSTYCH w/h → zaokrąglamy w dół.

    ``max_width`` (tor software): skaluje do co najwyżej tylu pikseli szerokości (``min(max,iw)``
    — nigdy w górę), wysokość proporcjonalnie i parzysto (``-2``). Kolejność: crop NAJPIERW, scale
    PO (skalujemy wycięty region). Przecinek w ``min(...)`` chroniony ``''`` (jeden łańcuch -vf).

    BEZ ``trim`` głowy: cięcie samego wideo rozjeżdżało A/V (audio nieprzycinane). Głowę
    (transient zimnego startu ddagrab) maskuje pre-roll w GUI (odczekanie), nie ffmpeg.
    """
    parts = ["hwdownload", "format=bgra"]
    if region is not None:
        x, y, w, h = (_even(v) for v in region)
        if w <= 0 or h <= 0:
            raise ValueError(f"Region niepoprawny po zaokrągleniu: {w}x{h}")
        parts.append(f"crop={w}:{h}:{x}:{y}")
    if max_width is not None:
        parts.append(f"scale='min({max_width},iw)':-2")
    parts.append("format=yuv420p")
    return ",".join(parts)


# Górny limit szerokości dla toru software (1080p). 60 fps@natywnej na libx264 = dropy na słabym
# CPU; zejście rozdzielczości jest — obok fps ≤ 30 — warunkiem używalności software-toru.
SOFTWARE_MAX_WIDTH = 1920


def encoder_label(choice: EncoderChoice) -> str:
    """Etykieta wybranego enkodera do LogView/RecordDialog. GUI nie ukrywa degradacji na software.

    Sprzęt: „nazwa (GPU)". Software: „nazwa (CPU) — ograniczono do 30 fps / 1080p" (ta sama
    informacja o obniżce fps/rozdzielczości, co realnie wymusza :func:`build_record_command`).
    """
    if choice.hardware:
        return f"{choice.name} (GPU)"
    return f"{choice.name} (CPU) — ograniczono do 30 fps / 1080p"


def resolve_encoder(quality: QualityOption, encoders: Mapping[str, bool]) -> EncoderChoice | None:
    """Wybór enkodera dla presetu (None dla audio-only). Wspólne źródło dla komendy i etykiety."""
    if quality.audio_only:
        return None
    return select_video_encoder(quality.video_codec or "h264", encoders)


def _video_input_args(source: CaptureSource, fps: int) -> list[str]:
    """Argumenty wejścia wideo dla ``ddagrab`` (Desktop Duplication API, GPU).

    Łapie cały OUTPUT (monitor) wskazany przez ``output_idx`` = ``source.monitor`` (hook
    konfiguracyjny pod hybrydę iGPU+dGPU; domyślnie 0). ddagrab oddaje tekstury D3D11 —
    pobranie do pamięci systemowej (``hwdownload``) robi filtr w :func:`build_record_command`.
    Pod-region (``region``) i okno po tytule nie są wspierane przez ddagrab → łapiemy monitor.
    ``-use_wallclock_as_timestamps 1`` (ten sam znacznik na wejściu audio) daje OBU strumieniom
    wspólny zegar czasu rzeczywistego → muxer trzyma realną relację A/V. ``-thread_queue_size``
    powiększa kolejkę wejścia, żeby drugi strumień nie głodził tego (dropy).
    """
    return [
        "-use_wallclock_as_timestamps",
        "1",
        "-thread_queue_size",
        "1024",
        "-f",
        "lavfi",
        "-i",
        f"ddagrab=output_idx={source.monitor}:framerate={fps}",
    ]


def _audio_devices(audio: AudioConfig) -> list[str]:
    """Lista urządzeń ``dshow`` do podłączenia (loopback systemu i/lub mikrofon)."""
    devices: list[str] = []
    if audio.system_audio and audio.system_device:
        devices.append(audio.system_device)
    if audio.microphone and audio.mic_device:
        devices.append(audio.mic_device)
    return devices


def build_record_command(
    *,
    source: CaptureSource,
    audio: AudioConfig,
    quality: QualityOption,
    encoders: Mapping[str, bool],
    segment_pattern: str,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    segment_start_number: int = 0,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Buduje pełną komendę FFmpeg nagrywania z segmentacją (crash-safe).

    Segmentacja (``-f segment``) finalizuje każdy segment przy rotacji, więc crash gubi
    tylko bieżący segment — wcześniejsze są kompletne i odtwarzalne. ``segment_pattern``
    musi zawierać wzorzec numeru (``%03d``) i właściwe rozszerzenie kontenera.

    Args:
        source: źródło wideo (ignorowane przy audio-only).
        audio: konfiguracja audio.
        quality: preset jakości (kodeki, fps, bitrate, audio_only).
        encoders: dostępne enkodery FFmpeg (do wyboru NVENC vs programowy).
        segment_pattern: wzorzec ścieżki segmentu, np. ``".../seg_%03d.mkv"``.
        segment_seconds: długość segmentu w sekundach.
        segment_start_number: numer pierwszego segmentu (rośnie po wznowieniu z pauzy).
        ffmpeg: nazwa/ścieżka binarki.

    Returns:
        Lista argumentów gotowa dla ``subprocess.Popen``.
    """
    audio_only = quality.audio_only
    devices = _audio_devices(audio)
    # Enkoder rozstrzygamy PRZED wejściem wideo — od tego, czy tor jest sprzętowy, zależy fps
    # (software ≤ 30) i skala (software ≤ 1920), a więc parametry samego wejścia ddagrab.
    choice = resolve_encoder(quality, encoders)
    software = choice is not None and not choice.hardware
    cmd: list[str] = [ffmpeg, "-hide_banner", "-y"]

    if not audio_only:
        # Sprzęt: min. 60 fps (steady-state płynny). Software: ≤ 30 fps (libx264 nie wyrobi 60).
        cmd += _video_input_args(source, record_framerate(quality.fps, software=software))
    for device in devices:
        # audio_buffer_size 50: tnie ~500 ms paczki dshow na ~50 ms → koniec non-monotonic DTS
        # (skoków znaczników → klampowania → dropów wideo). Wspólny wallclock + thread_queue.
        cmd += [
            "-use_wallclock_as_timestamps",
            "1",
            "-thread_queue_size",
            "1024",
            "-audio_buffer_size",
            "50",
            "-f",
            "dshow",
            "-i",
            f"audio={device}",
        ]

    # Mapowanie strumieni + tor audio. aresample=async=1 wygładza jitter dshow; przy >1 źródle
    # aresample PER WEJŚCIE (przed amix albo osobnymi śladami). Wspólny wallclock trzyma A/V.
    base = 0 if audio_only else 1  # indeks pierwszego wejścia audio (po ewentualnym wideo)
    mixing = audio.mix and len(devices) > 1
    if len(devices) > 1:
        chains = ";".join(f"[{base + i}:a]aresample=async=1[a{i}]" for i in range(len(devices)))
        if mixing:
            labels = "".join(f"[a{i}]" for i in range(len(devices)))
            chains = f"{chains};{labels}amix=inputs={len(devices)}:normalize=0[aout]"
        cmd += ["-filter_complex", chains]

    if not audio_only:
        cmd += ["-map", "0:v"]
    if len(devices) == 1:
        cmd += ["-map", f"{base}:a", "-af", "aresample=async=1"]
    elif mixing:
        cmd += ["-map", "[aout]"]
    elif len(devices) > 1:  # osobne ślady bez miksu
        cmd += [arg for i in range(len(devices)) for arg in ("-map", f"[a{i}]")]

    # Kodek wideo (sprzęt z fallbackiem na libx264) + adaptacja software + bitrate.
    if not audio_only:
        assert choice is not None  # not audio_only → resolve_encoder zwrócił wybór
        # ddagrab oddaje tekstury GPU → pobierz do RAM (+ crop regionu), ustaw format (bez trim).
        # Software: dodatkowo scale ≤ 1920 (crop najpierw, scale po) — bez tego libx264 gubi klatki.
        cmd += [
            "-vf",
            build_video_filter(source.region, max_width=SOFTWARE_MAX_WIDTH if software else None),
        ]
        cmd += ["-c:v", choice.name]
        # Flagi jakości PER RODZINA enkodera (nvenc/qsv/amf/libx264) — presety p1-p7 są tylko
        # w NVENC, więc doklejanie ich na sztywno wywalało fallback na QSV/AMF (patrz funkcja).
        cmd += encoder_quality_args(choice.name)
        if quality.bitrate_kbps:
            cmd += ["-b:v", f"{quality.bitrate_kbps}k"]
        cmd += ["-fps_mode", "cfr"]  # stały framerate → koniec skokowości w pliku

    # Kodek audio.
    if devices:
        cmd += ["-c:a", quality.audio_codec or audio.audio_codec or "aac"]
        cmd += ["-b:a", f"{audio.audio_bitrate_kbps}k"]

    # Segmentacja crash-safe.
    cmd += [
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_start_number",
        str(segment_start_number),
        "-reset_timestamps",
        "1",
        segment_pattern,
    ]
    return cmd


def estimate_size_mb(quality: QualityOption, seconds: float, audio: AudioConfig) -> float:
    """Szacuje rozmiar pliku w MB z bitrate wideo + audio i czasu trwania.

    Czysto orientacyjne (target bitrate x czas), do podglądu w GUI.
    """
    # audio_only bez żadnego urządzenia: nagranie w ogóle nie ruszy (brak -i → FFmpeg pada
    # natychmiast, M3), więc podgląd „0 MB" jest uczciwy — zamiast domyślać się 1 śladu 160 kb/s.
    if quality.audio_only and not _audio_devices(audio):
        return 0.0
    video_kbps = 0 if quality.audio_only else (quality.bitrate_kbps or 0)
    track_count = len(_audio_devices(audio)) or (1 if quality.audio_only else 0)
    audio_kbps = audio.audio_bitrate_kbps * (1 if audio.mix else track_count)
    total_kbps = video_kbps + audio_kbps
    return round(total_kbps * seconds / 8 / 1024, 1)


@dataclass(slots=True)
class RecorderPlan:
    """Rozstrzygnięty plan nagrania (do podglądu/logu przed startem)."""

    encoder: EncoderChoice | None
    audio_tracks: int
    fps: int
    container: str
    command: list[str] = field(default_factory=list)
