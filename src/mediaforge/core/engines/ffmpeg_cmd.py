"""Budowa komend FFmpeg dla nagrywania ekranu/audio — czysta logika (bez subprocess).

Wydzielone od orkiestracji (:mod:`.recorder`), żeby budowanie komend było w pełni
testowalne bez uruchamiania FFmpeg ani bycia na Windows. Funkcje są deterministyczne:
te same wejścia → ta sama lista argumentów.

Cel platformowy to Windows (nagrywanie): wideo przez ``gdigrab`` (cały pulpit /
region / okno po tytule), audio przez ``dshow`` (urządzenie WASAPI loopback dla
dźwięku systemowego oraz mikrofon). Wybór monitora realizuje GUI, podając region w
pikselach fizycznych (DPI-aware) — tu monitor = region.

NVENC (HEVC/AV1) z fallbackiem programowym: :func:`select_video_encoder` schodzi po
łańcuchu preferencji do pierwszego enkodera obecnego w buildzie FFmpeg
(``check_ffmpeg()['encoders']``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from mediaforge.core.engines.base import QualityOption


class CaptureMode(StrEnum):
    """Tryb źródła wideo."""

    FULLSCREEN = "fullscreen"  # cały pulpit (lub wybrany monitor jako region)
    REGION = "region"  # prostokąt x,y,w,h
    WINDOW = "window"  # konkretne okno po tytule


@dataclass(slots=True)
class CaptureSource:
    """Co nagrywamy: pełny ekran / monitor (region) / okno.

    ``monitor`` i ``region`` ustala GUI (DPI-aware, piksele fizyczne). Wybór monitora
    sprowadza się do regionu = geometria tego monitora, więc FFmpeg dostaje offset+size.
    """

    mode: CaptureMode = CaptureMode.FULLSCREEN
    monitor: int = 0
    region: tuple[int, int, int, int] | None = None  # x, y, w, h (piksele fizyczne)
    window_title: str | None = None


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

# Łańcuchy preferencji enkoderów: (nazwa, czy_sprzętowy). NVENC najpierw, potem programowy.
_CODEC_CHAINS: dict[str, list[tuple[str, bool]]] = {
    "hevc": [("hevc_nvenc", True), ("libx265", False), ("h264_nvenc", True), ("libx264", False)],
    "av1": [
        ("av1_nvenc", True),
        ("libsvtav1", False),
        ("libaom-av1", False),
        ("hevc_nvenc", True),
        ("libx265", False),
        ("h264_nvenc", True),
        ("libx264", False),
    ],
    "h264": [("h264_nvenc", True), ("libx264", False), ("hevc_nvenc", True), ("libx265", False)],
}


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
    # Łańcuch pusty w tym buildzie — weź cokolwiek dostępnego (deterministycznie: wg łańcucha).
    for name, available in encoders.items():
        if available:
            return EncoderChoice(name, name.endswith("nvenc"))
    return EncoderChoice("libx264", False)


def _video_input_args(source: CaptureSource, fps: int) -> list[str]:
    """Argumenty wejścia wideo dla ``gdigrab`` (okno po tytule albo pulpit/region)."""
    args = ["-f", "gdigrab", "-framerate", str(fps)]
    if source.mode is CaptureMode.WINDOW and source.window_title:
        args += ["-i", f"title={source.window_title}"]
        return args
    region = source.region
    if source.mode is CaptureMode.REGION and region is not None:
        x, y, w, h = region
        args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
    args += ["-i", "desktop"]
    return args


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
    cmd: list[str] = [ffmpeg, "-hide_banner", "-y"]

    if not audio_only:
        cmd += _video_input_args(source, quality.fps or 30)
    for device in devices:
        cmd += ["-f", "dshow", "-i", f"audio={device}"]

    # Mapowanie + miks audio.
    mixing = audio.mix and len(devices) > 1
    if mixing:
        # Indeksy wejść audio zaczynają się po (ewentualnym) wejściu wideo.
        base = 0 if audio_only else 1
        inputs = "".join(f"[{base + i}:a]" for i in range(len(devices)))
        cmd += ["-filter_complex", f"{inputs}amix=inputs={len(devices)}:normalize=0[aout]"]
        if not audio_only:
            cmd += ["-map", "0:v", "-map", "[aout]"]
        else:
            cmd += ["-map", "[aout]"]
    else:
        if not audio_only:
            cmd += ["-map", "0:v"]
        for i in range(len(devices)):
            idx = i if audio_only else i + 1
            cmd += ["-map", f"{idx}:a"]

    # Kodek wideo (NVENC z fallbackiem) + bitrate.
    if not audio_only:
        choice = select_video_encoder(quality.video_codec or "h264", encoders)
        cmd += ["-c:v", choice.name]
        if choice.hardware:
            cmd += ["-preset", "p5"]  # zbalansowany preset NVENC
        if quality.bitrate_kbps:
            cmd += ["-b:v", f"{quality.bitrate_kbps}k"]
        cmd += ["-pix_fmt", "yuv420p"]

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
