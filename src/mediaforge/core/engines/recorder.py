"""RecorderEngine — nagrywanie ekranu/audio przez FFmpeg (implementacja AcquisitionEngine).

Orkiestracja nad czystą logiką: budowanie komend (:mod:`.ffmpeg_cmd`) i odzysk
zsegmentowanych nagrań (:mod:`.segments`). FFmpeg działa jako **subprocess** (licencja
MIT + izolacja crashu, zob. CLAUDE.md), wstrzykiwany przez fabrykę procesu, więc cała
maszyna stanów (start/pauza/wznowienie/stop) jest testowalna bez uruchamiania FFmpeg.

Crash-safe: nagranie idzie w segmentach finalizowanych przy rotacji. Pauza zatrzymuje
bieżący proces (z numeracją segmentów kontynuowaną po wznowieniu), stop skleja ważne
segmenty w jeden plik (``concat``, kopia strumieni). Po zakończeniu powstaje folder
materiału + wpis w bibliotece (status ``recorded``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol

from mediaforge.core.engines import segments
from mediaforge.core.engines.base import (
    AcquireOptions,
    MediaArtifact,
    QualityOption,
    Source,
    SourceKind,
)
from mediaforge.core.engines.ffmpeg_cmd import (
    DEFAULT_SEGMENT_SECONDS,
    PRESETS,
    AudioConfig,
    CaptureMode,
    CaptureSource,
    build_record_command,
    estimate_size_mb,
)
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStatus, RecordingStore
from mediaforge.core.winutil import NO_WINDOW_FLAGS


class RecorderState(StrEnum):
    """Stan sesji nagrywania."""

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPED = "stopped"


class RecorderProcess(Protocol):
    """Minimalny kontrakt procesu FFmpeg potrzebny sesji (pozwala na atrapę w testach)."""

    def stop_gracefully(self, timeout: float = 8.0) -> None:
        """Poproś FFmpeg o domknięcie bieżącego segmentu (``q``), z fallbackiem terminate."""
        ...

    def is_running(self) -> bool:
        """Czy proces wciąż żyje."""
        ...


ProcessFactory = Callable[[list[str], Path | None], RecorderProcess]
ConcatRunner = Callable[[list[str]], int]


class _FfmpegProcess:
    """Realny proces FFmpeg: graceful stop przez ``q`` na stdin, fallback terminate/kill.

    stderr FFmpeg trafia do ``log_path`` (tryb append — kolejne legi dopisują), zamiast do
    ``/dev/null``. Dzięki temu po nagłej śmierci procesu zostaje ślad diagnostyczny (kod błędu
    urządzenia, brak enkodera, itp.). Plik żyje w ``_work``, więc sprząta się razem z segmentami.
    """

    def __init__(self, command: list[str], log_path: Path | None = None) -> None:
        # Uchwyt trzyma proces przez cały czas życia (zamykany w stop_gracefully) — świadomie
        # bez context managera. Append: kolejne legi dopisują do tego samego ffmpeg.log.
        self._log_file: IO[bytes] | None = None
        if log_path is not None:
            self._log_file = log_path.open("ab")
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file or subprocess.DEVNULL,
            creationflags=NO_WINDOW_FLAGS,
        )

    def stop_gracefully(self, timeout: float = 8.0) -> None:
        # FFmpeg domyka muxer po otrzymaniu 'q' na stdin — segment zostaje poprawny.
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        finally:
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None

    def is_running(self) -> bool:
        return self._proc.poll() is None


def _default_process_factory(command: list[str], log_path: Path | None = None) -> RecorderProcess:
    return _FfmpegProcess(command, log_path)


def _default_concat_runner(command: list[str]) -> int:
    proc = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW_FLAGS,
        check=False,
    )
    return proc.returncode


_SLUG_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_filename(name: str, *, fallback: str = "nagranie") -> str:
    """Sanityzuje nazwę pliku pod Windows (usuwa znaki zabronione, przycina kropki/spacje)."""
    cleaned = _SLUG_RE.sub("_", name).strip(" .")
    return cleaned or fallback


# ── Kolizja nazwy materiału (folder = źródło prawdy) ──────────────────────────


def material_dir_for(output_dir: Path, title: str) -> Path:
    """Folder materiału dla nazwy — ta sama konwencja co :meth:`finalize_to_library`."""
    return output_dir / safe_filename(title)


def work_dir_for(output_dir: Path, title: str) -> Path:
    """Katalog roboczy nagrania: podkatalog ``_work`` w folderze materiału — JEDNO źródło konwencji.

    Segmenty muszą trafiać do podkatalogu, NIE do samego folderu materiału: finalize sklei je do
    ``<material>/<slug>.<ext>`` obok ``_work``. Gdyby ``work_dir`` == folder materiału, segmenty i
    plik wynikowy mieszałyby się, a kolejne nagranie nadpisałoby folder (utrata danych).
    """
    return material_dir_for(output_dir, title) / "_work"


def material_exists(output_dir: Path, title: str) -> bool:
    """Czy materiał o tej nazwie już istnieje (folder z ``metadata.json`` = ukończony materiał)."""
    return (material_dir_for(output_dir, title) / "metadata.json").is_file()


def next_free_title(output_dir: Path, title: str) -> str:
    """Pierwsza wolna nazwa: sama ``title``, albo ``title (2)``, ``title (3)``, … (wolny folder)."""
    candidate = title
    n = 1
    while material_exists(output_dir, candidate):
        n += 1
        candidate = f"{title} ({n})"
    return candidate


def discard_material_dir(output_dir: Path, title: str) -> None:
    """Usuwa cały folder materiału (plik + segmenty ``_work`` + transcript + metadata) — nadpisanie.

    SQLite odświeża potem ``upsert_material`` w :meth:`finalize_to_library` (ten sam folder =
    ten sam wiersz, bez duplikatu; świeże metadane zerują ``transcript_status``).
    """
    material_dir = material_dir_for(output_dir, title)
    if material_dir.exists():
        shutil.rmtree(material_dir, ignore_errors=True)


@dataclass(slots=True)
class RecorderStatus:
    """Migawka stanu sesji do GUI (timer + szacowany rozmiar)."""

    state: RecorderState
    elapsed_seconds: float
    estimated_mb: float
    segment_count: int


class RecorderSession:
    """Maszyna stanów nagrywania: start → (pauza ⇄ wznowienie)* → stop → finalizacja.

    Każde start/wznowienie spawnuje nowy proces FFmpeg piszący kolejne segmenty (numeracja
    ciągła). Stop skleja ważne segmenty w jeden plik. Zegar i fabryka procesu są wstrzykiwane,
    więc cała logika jest deterministyczna i testowalna bez FFmpeg.
    """

    def __init__(
        self,
        *,
        source: CaptureSource,
        audio: AudioConfig,
        quality: QualityOption,
        work_dir: Path,
        encoders: dict[str, bool],
        segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
        process_factory: ProcessFactory = _default_process_factory,
        concat_runner: ConcatRunner = _default_concat_runner,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.audio = audio
        self.quality = quality
        self.work_dir = work_dir
        self.encoders = encoders
        self.segment_seconds = segment_seconds
        self._process_factory = process_factory
        self._concat_runner = concat_runner
        self._clock = clock
        self.container = "mka" if quality.audio_only else "mkv"

        self.state = RecorderState.IDLE
        self._proc: RecorderProcess | None = None
        self._elapsed_before = 0.0  # zsumowany czas z poprzednich odcinków (przed pauzami)
        self._leg_started_at: float | None = None

    # ── Sterowanie ──────────────────────────────────────────────────────────────

    def _spawn(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        command = build_record_command(
            source=self.source,
            audio=self.audio,
            quality=self.quality,
            encoders=self.encoders,
            segment_pattern=segments.segment_pattern(self.work_dir, self.container),
            segment_seconds=self.segment_seconds,
            segment_start_number=self._next_segment_number(),
        )
        self._proc = self._process_factory(command, self.work_dir / "ffmpeg.log")
        self._leg_started_at = self._clock()

    def _next_segment_number(self) -> int:
        """Numer kolejnego segmentu = liczba już istniejących (ciągłość po wznowieniu)."""
        return len(segments.list_segments(self.work_dir))

    def start(self) -> None:
        """Rozpoczyna nagrywanie. Dozwolone tylko ze stanu IDLE."""
        if self.state is not RecorderState.IDLE:
            raise RuntimeError(f"start() niedozwolony w stanie {self.state}")
        self._clear_work_dir()
        self._spawn()
        self.state = RecorderState.RECORDING

    def _clear_work_dir(self) -> None:
        """Czyści katalog roboczy PRZED nową sesją: stare ``seg_*.mkv`` skleiłyby się z nowymi
        (concat po stop) → dwie zmieszane sesje w jednym pliku. Tylko ze :meth:`start`
        (świadomy start), NIE z :meth:`resume` — wznowienie kontynuuje numerację segmentów.
        Odzysk po crashu (jeśli dodany) biegnie wcześniej, przy otwarciu dialogu — nie tu.
        """
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def pause(self) -> None:
        """Wstrzymuje: domyka bieżący proces FFmpeg, zlicza czas odcinka."""
        if self.state is not RecorderState.RECORDING:
            raise RuntimeError(f"pause() niedozwolony w stanie {self.state}")
        self._finish_leg()
        self.state = RecorderState.PAUSED

    def resume(self) -> None:
        """Wznawia: spawnuje nowy proces kontynuujący numerację segmentów."""
        if self.state is not RecorderState.PAUSED:
            raise RuntimeError(f"resume() niedozwolony w stanie {self.state}")
        self._spawn()
        self.state = RecorderState.RECORDING

    def stop(self) -> None:
        """Kończy nagrywanie: domyka proces i przechodzi w STOPPED (bez sklejania)."""
        if self.state not in (RecorderState.RECORDING, RecorderState.PAUSED):
            raise RuntimeError(f"stop() niedozwolony w stanie {self.state}")
        if self.state is RecorderState.RECORDING:
            self._finish_leg()
        self.state = RecorderState.STOPPED

    def _finish_leg(self) -> None:
        """Domyka bieżący proces i dolicza czas trwania odcinka do sumy."""
        if self._proc is not None:
            self._proc.stop_gracefully()
            self._proc = None
        if self._leg_started_at is not None:
            self._elapsed_before += max(0.0, self._clock() - self._leg_started_at)
            self._leg_started_at = None

    # ── Wykrycie śmierci procesu ──────────────────────────────────────────────────

    def process_alive(self) -> bool:
        """Czy proces FFmpeg bieżącego odcinka wciąż żyje (do wykrycia nagłej śmierci w GUI)."""
        return self._proc is not None and self._proc.is_running()

    def read_process_log_tail(self, lines: int = 8) -> str:
        """Końcówka ``ffmpeg.log`` (ostatnie ``lines`` linii) — diagnostyka po śmierci procesu.

        Odpornie: brak pliku (atrapa / proces nie zdążył nic zapisać) → pusty string.
        """
        try:
            content = (self.work_dir / "ffmpeg.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def mark_process_died(self) -> None:
        """Odnotowuje nagłą śmierć procesu FFmpeg: przechodzi w PAUSED BEZ ``stop_gracefully``.

        W odróżnieniu od :meth:`pause` nie prosi martwego już procesu o domknięcie — jedynie
        dolicza czas bieżącego odcinka i porzuca uchwyt. Segmenty na dysku są nienaruszone, więc
        użytkownik może wznowić (nowy proces, ciągła numeracja) albo zatrzymać (finalizacja tego,
        co jest) — to cała siła architektury segmentowej.
        """
        if self.state is not RecorderState.RECORDING:
            raise RuntimeError(f"mark_process_died() niedozwolony w stanie {self.state}")
        self._proc = None  # proces martwy — nie wołamy stop_gracefully
        if self._leg_started_at is not None:
            self._elapsed_before += max(0.0, self._clock() - self._leg_started_at)
            self._leg_started_at = None
        self.state = RecorderState.PAUSED

    # ── Telemetria ──────────────────────────────────────────────────────────────

    @property
    def elapsed_seconds(self) -> float:
        """Łączny czas nagrania (suma odcinków, bez czasu pauzy)."""
        running = 0.0
        if self.state is RecorderState.RECORDING and self._leg_started_at is not None:
            running = max(0.0, self._clock() - self._leg_started_at)
        return self._elapsed_before + running

    def status(self) -> RecorderStatus:
        """Migawka stanu do GUI (timer + szacowany rozmiar + liczba segmentów)."""
        elapsed = self.elapsed_seconds
        return RecorderStatus(
            state=self.state,
            elapsed_seconds=elapsed,
            estimated_mb=estimate_size_mb(self.quality, elapsed, self.audio),
            segment_count=len(segments.list_segments(self.work_dir)),
        )

    # ── Finalizacja ─────────────────────────────────────────────────────────────

    def finalize(self, output: Path) -> tuple[segments.RecoveryResult, int | None]:
        """Skleja ważne segmenty w ``output`` (concat). Zwraca (plan odzysku, kod wyjścia concat).

        Bezpieczne także po crashu: opiera się o segmenty na dysku, nie o stan procesu.
        Uruchamia FFmpeg concat tylko, gdy jest cokolwiek do sklejenia — inaczej kod wyjścia
        to ``None`` (concat nie ruszył). Kod wyjścia pozwala wołającemu odróżnić porażkę sklejania.
        """
        plan = segments.plan_recovery(self.work_dir, output, ffmpeg="ffmpeg")
        returncode: int | None = None
        if plan.recoverable:
            returncode = self._concat_runner(plan.command)
        return plan, returncode


@dataclass(slots=True)
class RecorderEngine:
    """Silnik akwizycji dla nagrywania ekranu (Protocol :class:`AcquisitionEngine`).

    GUI używa :meth:`new_session` (interaktywne start/pauza/stop) oraz :meth:`finalize_to_library`.
    :meth:`acquire` to wygodne, blokujące nagranie (CLI/testy): nagrywa do ustawienia
    ``stop_event`` lub upływu ``max_seconds``.
    """

    encoders: dict[str, bool]
    store: RecordingStore | None = None
    process_factory: ProcessFactory = _default_process_factory
    concat_runner: ConcatRunner = _default_concat_runner
    name: str = "recorder"

    def can_handle(self, source: Source) -> bool:
        """Obsługuje wyłącznie źródła ekranowe (nagrywanie)."""
        return source.kind is SourceKind.SCREEN

    def probe(self, source: Source) -> list[QualityOption]:
        """Dostępne presety jakości (te same niezależnie od źródła ekranowego)."""
        return list(PRESETS.values())

    def new_session(
        self,
        *,
        source: CaptureSource,
        audio: AudioConfig,
        quality: QualityOption,
        work_dir: Path,
        segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    ) -> RecorderSession:
        """Tworzy sesję nagrywania (do interaktywnego sterowania z GUI)."""
        return RecorderSession(
            source=source,
            audio=audio,
            quality=quality,
            work_dir=work_dir,
            encoders=self.encoders,
            segment_seconds=segment_seconds,
            process_factory=self.process_factory,
            concat_runner=self.concat_runner,
        )

    def finalize_to_library(
        self,
        session: RecorderSession,
        *,
        title: str,
        output_dir: Path,
    ) -> MediaArtifact:
        """Skleja nagranie do folderu materiału i (jeśli jest store) dodaje wpis ``recorded``.

        Folder materiału = ``output_dir/<bezpieczna-nazwa>``; plik wynikowy w środku.
        Zwraca :class:`MediaArtifact` z metadanymi (czas trwania, liczba segmentów).

        Nie tworzy wpisu (ani ``metadata.json``, ani wiersza SQLite) bez realnego pliku wynikowego:
        brak ważnych segmentów lub nieudane sklejanie → :class:`RuntimeError`. Segmenty w ``_work``
        zostają nietknięte do ręcznego odzysku.
        """
        slug = safe_filename(title)
        material_dir = output_dir / slug
        material_dir.mkdir(parents=True, exist_ok=True)
        output = material_dir / f"{slug}.{session.container}"
        plan, concat_rc = session.finalize(output)

        # Brak ważnych segmentów → nie ma czego zapisywać (nie piszemy metadata.json ani upsert).
        if not plan.recoverable:
            raise RuntimeError(
                "Brak ważnych segmentów — nagranie nie zostało zapisane (sprawdź log FFmpeg)."
            )
        # Concat ruszył, ale plik wynikowy nie powstał / jest pusty → porażka sklejania.
        # Segmenty zostają w _work do ręcznego odzysku; nie tworzymy wpisu w bibliotece.
        if not (output.is_file() and output.stat().st_size > 0):
            raise RuntimeError(
                f"Sklejanie segmentów nie powiodło się (kod wyjścia concat: {concat_rc}) — "
                f"segmenty zostają w {session.work_dir}"
            )

        is_audio = session.quality.audio_only
        video_path = None if is_audio else output
        audio_path = output if is_audio else None
        duration = round(session.elapsed_seconds, 1)

        # Ten sam układ co import: metadata.json (źródło prawdy) + wpis w SQLite.
        meta = MaterialMetadata(
            title=title,
            created_at=datetime.now(UTC).isoformat(),
            source_type="screen",
            duration=duration,
            video_path=None if is_audio else output.name,
            audio_path=output.name if is_audio else None,
            status=RecordingStatus.RECORDED.value,
        )
        write_metadata(material_dir, meta)
        if self.store is not None:
            self.store.upsert_material(material_dir, meta)
        return MediaArtifact(
            video_path=video_path,
            audio_path=audio_path,
            metadata={
                "duration_s": str(duration),
                "segments": str(len(plan.segments)),
                "recoverable": str(plan.recoverable),
            },
        )

    def acquire(
        self,
        source: Source,
        opts: AcquireOptions,
        progress: Callable[[float, str], None],
        *,
        stop_event: object | None = None,
        max_seconds: float | None = None,
    ) -> MediaArtifact:
        """Blokujące nagranie pełnego ekranu + dźwięku systemowego (CLI/testy).

        Nagrywa do ustawienia ``stop_event`` (``threading.Event``) albo upływu
        ``max_seconds``. GUI nie używa tej metody — steruje sesją bezpośrednio.
        """
        capture = CaptureSource(mode=CaptureMode.FULLSCREEN)
        audio = AudioConfig(system_audio=True)
        # Ochrona kolizji jak w GUI: kolejne nagranie idzie do „nagranie (2)", nie nadpisuje.
        title = next_free_title(opts.output_dir, "nagranie")
        session = self.new_session(
            source=capture,
            audio=audio,
            quality=opts.quality,
            work_dir=work_dir_for(opts.output_dir, title),  # podkatalog _work, nie folder materiału
        )
        session.start()
        progress(0.0, "Nagrywanie rozpoczęte")
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        while session.state is RecorderState.RECORDING:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        session.stop()
        progress(0.95, "Składanie segmentów")
        artifact = self.finalize_to_library(session, title=title, output_dir=opts.output_dir)
        progress(1.0, "Zakończono")
        return artifact
