"""Testy maszyny stanów nagrywania (start/pauza/wznowienie/stop) z atrapą FFmpeg."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mediaforge.core.engines import segments
from mediaforge.core.engines.base import AcquireOptions, SourceKind
from mediaforge.core.engines.base import Source as EngineSource
from mediaforge.core.engines.ffmpeg_cmd import PRESETS, AudioConfig, CaptureSource
from mediaforge.core.engines.recorder import (
    RecorderEngine,
    RecorderSession,
    RecorderState,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.recordings import RecordingStatus, RecordingStore

_ENCODERS = {"hevc_nvenc": True, "libx265": True, "libx264": True}


def _state(session: RecorderSession) -> RecorderState:
    """Świeży odczyt stanu (omija zawężanie typu przez mypy między wywołaniami)."""
    return session.state


class _Clock:
    """Sterowany zegar do deterministycznego pomiaru czasu nagrania."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeProc:
    def stop_gracefully(self, timeout: float = 8.0) -> None:
        return None

    def is_running(self) -> bool:
        return False


def _fake_factory(spawned: list[list[str]]) -> Callable[[list[str]], _FakeProc]:
    """Fabryka atrapy: tworzy plik segmentu wg numeru z komendy (symuluje FFmpeg)."""

    def factory(command: list[str]) -> _FakeProc:
        pattern = command[-1]
        start = int(command[command.index("-segment_start_number") + 1])
        Path(pattern % start).write_bytes(b"SEGMENT")
        spawned.append(command)
        return _FakeProc()

    return factory


def _fake_concat(output_holder: list[Path]) -> Callable[[list[str]], int]:
    def runner(command: list[str]) -> int:
        Path(command[-1]).write_bytes(b"FINAL")
        output_holder.append(Path(command[-1]))
        return 0

    return runner


def test_session_pause_resume_continues_segment_numbering(tmp_path: Path) -> None:
    spawned: list[list[str]] = []
    clock = _Clock()
    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "work",
        encoders=_ENCODERS,
        process_factory=_fake_factory(spawned),
        clock=clock,
    )

    session.start()
    assert _state(session) is RecorderState.RECORDING
    clock.advance(5.0)
    session.pause()
    assert _state(session) is RecorderState.PAUSED
    session.resume()
    clock.advance(3.0)
    session.stop()
    assert _state(session) is RecorderState.STOPPED

    # Dwa odcinki → dwa procesy, segmenty 000 i 001 (ciągła numeracja).
    assert len(spawned) == 2
    names = sorted(p.name for p in segments.list_segments(tmp_path / "work"))
    assert names == ["seg_000.mkv", "seg_001.mkv"]
    # Czas = suma odcinków (bez pauzy): 5 + 3.
    assert session.elapsed_seconds == 8.0


def test_session_finalize_concats_segments(tmp_path: Path) -> None:
    spawned: list[list[str]] = []
    outputs: list[Path] = []
    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "work",
        encoders=_ENCODERS,
        process_factory=_fake_factory(spawned),
        concat_runner=_fake_concat(outputs),
    )
    session.start()
    session.stop()
    out = tmp_path / "final.mkv"
    plan = session.finalize(out)

    assert plan.recoverable is True
    assert out.exists()
    assert outputs == [out]


def test_invalid_transitions_raise(tmp_path: Path) -> None:
    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "work",
        encoders=_ENCODERS,
        process_factory=_fake_factory([]),
    )
    # pause przed start
    try:
        session.pause()
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("pause() powinno rzucić w stanie IDLE")


def test_engine_acquire_creates_library_entry(tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    Database(db_path).migrate()
    store = RecordingStore(db_path)
    outputs: list[Path] = []
    engine = RecorderEngine(
        encoders=_ENCODERS,
        store=store,
        process_factory=_fake_factory([]),
        concat_runner=_fake_concat(outputs),
    )

    artifact = engine.acquire(
        EngineSource(kind=SourceKind.SCREEN, target="desktop"),
        AcquireOptions(quality=PRESETS["standard"], output_dir=tmp_path / "out"),
        lambda frac, msg: None,
        max_seconds=0.0,
    )

    assert artifact.video_path is not None
    recordings = store.list_recordings(RecordingStatus.RECORDED)
    assert len(recordings) == 1
    assert recordings[0].status is RecordingStatus.RECORDED


def test_engine_can_handle_only_screen() -> None:
    engine = RecorderEngine(encoders=_ENCODERS)
    assert engine.can_handle(EngineSource(kind=SourceKind.SCREEN, target="desktop")) is True
    assert engine.can_handle(EngineSource(kind=SourceKind.LOCAL_FILE, target="a.mp4")) is False
    assert len(engine.probe(EngineSource(kind=SourceKind.SCREEN, target="desktop"))) == 5
