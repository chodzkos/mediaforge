"""Testy maszyny stanów nagrywania (start/pauza/wznowienie/stop) z atrapą FFmpeg."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from mediaforge.core.engines import segments
from mediaforge.core.engines.base import AcquireOptions, SourceKind
from mediaforge.core.engines.base import Source as EngineSource
from mediaforge.core.engines.ffmpeg_cmd import PRESETS, AudioConfig, CaptureSource
from mediaforge.core.engines.import_engine import build_probe_duration_command
from mediaforge.core.engines.recorder import (
    RecorderEngine,
    RecorderSession,
    RecorderState,
    discard_material_dir,
    material_dir_for,
    next_free_title,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
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


def _fake_factory(spawned: list[list[str]]) -> Callable[[list[str], Path | None], _FakeProc]:
    """Fabryka atrapy: tworzy plik segmentu wg numeru z komendy (symuluje FFmpeg)."""

    def factory(command: list[str], log_path: Path | None = None) -> _FakeProc:
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


class _DyingProc:
    """Atrapa procesu umierającego po ``alive_calls`` sprawdzeniach ``is_running()``."""

    def __init__(self, alive_calls: int) -> None:
        self._left = alive_calls

    def stop_gracefully(self, timeout: float = 8.0) -> None:
        return None

    def is_running(self) -> bool:
        if self._left <= 0:
            return False
        self._left -= 1
        return True


def test_session_detects_process_death_and_marks_paused(tmp_path: Path) -> None:
    """Nagła śmierć FFmpeg: process_alive() False, mark_process_died() → PAUSED, czas doliczony."""
    clock = _Clock()
    proc = _DyingProc(alive_calls=1)  # żyje przy 1. sprawdzeniu, potem martwy

    def factory(command: list[str], log_path: Path | None = None) -> _DyingProc:
        pattern = command[-1]
        start = int(command[command.index("-segment_start_number") + 1])
        Path(pattern % start).write_bytes(b"SEGMENT")
        return proc

    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "work",
        encoders=_ENCODERS,
        process_factory=factory,
        clock=clock,
    )
    session.start()
    clock.advance(4.0)
    assert session.process_alive() is True  # 1. sprawdzenie — jeszcze żyje
    assert session.process_alive() is False  # proces padł

    session.mark_process_died()
    assert _state(session) is RecorderState.PAUSED
    assert session.elapsed_seconds == 4.0  # czas bieżącego odcinka doliczony
    # Segment na dysku przeżył — można wznowić (ciągła numeracja) albo zatrzymać.
    assert [p.name for p in segments.list_segments(tmp_path / "work")] == ["seg_000.mkv"]
    session.resume()
    assert _state(session) is RecorderState.RECORDING


def test_read_process_log_tail_missing_file_is_empty(tmp_path: Path) -> None:
    """Brak ffmpeg.log (atrapa nic nie zapisała) → pusty ogon (odpornie, bez wyjątku)."""
    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "work",
        encoders=_ENCODERS,
        process_factory=_fake_factory([]),
    )
    assert session.read_process_log_tail() == ""
    # Z plikiem: zwraca ostatnie N linii.
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "ffmpeg.log").write_text("a\nb\nc\nd\n", encoding="utf-8")
    assert session.read_process_log_tail(lines=2) == "c\nd"


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
    plan, rc = session.finalize(out)

    assert plan.recoverable is True
    assert rc == 0  # kod wyjścia concat
    assert out.exists()
    assert outputs == [out]


def _no_segment_factory(command: list[str], log_path: Path | None = None) -> _FakeProc:
    """Fabryka NIE tworząca pliku segmentu — symuluje FFmpeg padły przed jakimkolwiek zapisem."""
    return _FakeProc()


def test_finalize_without_segments_raises_and_writes_nothing(tmp_path: Path) -> None:
    """Brak ważnych segmentów → RuntimeError; ani metadata.json, ani wiersz w store."""
    db = tmp_path / "lib.sqlite3"
    Database(db).migrate()
    store = RecordingStore(db)
    engine = RecorderEngine(
        encoders=_ENCODERS,
        store=store,
        process_factory=_no_segment_factory,
        concat_runner=_fake_concat([]),
    )
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "out" / "Nagranie" / "_work",
    )
    session.start()
    session.stop()

    with pytest.raises(RuntimeError, match="Brak ważnych segmentów"):
        engine.finalize_to_library(session, title="Nagranie", output_dir=tmp_path / "out")

    assert not (tmp_path / "out" / "Nagranie" / "metadata.json").exists()
    assert store.list_materials() == []


def test_finalize_concat_failure_raises_and_writes_nothing(tmp_path: Path) -> None:
    """Concat zwraca ≠0 i nie tworzy pliku → RuntimeError; brak wpisu; segmenty zostają w _work."""
    db = tmp_path / "lib.sqlite3"
    Database(db).migrate()
    store = RecordingStore(db)

    def failing_concat(command: list[str]) -> int:
        return 1  # kod ≠ 0 i celowo NIE tworzy pliku wynikowego

    engine = RecorderEngine(
        encoders=_ENCODERS,
        store=store,
        process_factory=_fake_factory([]),  # tworzy segment (jest co sklejać)
        concat_runner=failing_concat,
    )
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "out" / "Nagranie" / "_work",
    )
    session.start()
    session.stop()

    with pytest.raises(RuntimeError, match="Sklejanie segmentów nie powiodło się"):
        engine.finalize_to_library(session, title="Nagranie", output_dir=tmp_path / "out")

    assert not (tmp_path / "out" / "Nagranie" / "metadata.json").exists()
    assert store.list_materials() == []
    # Po NIEUDANYM finalize _work zostaje (ręczny odzysk) — nie sprzątamy przed weryfikacją.
    assert session.work_dir.exists()
    assert list(segments.list_segments(session.work_dir))  # segmenty do ręcznego odzysku


def test_finalize_success_removes_work_dir(tmp_path: Path) -> None:
    """Po ZWERYFIKOWANYM sukcesie (plik istnieje, size>0) segmenty _work są sprzątane."""
    db = tmp_path / "lib.sqlite3"
    Database(db).migrate()
    store = RecordingStore(db)
    engine = RecorderEngine(
        encoders=_ENCODERS,
        store=store,
        process_factory=_fake_factory([]),  # tworzy segment
        concat_runner=_fake_concat([]),  # tworzy plik wynikowy (size>0)
    )
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "out" / "Nagranie" / "_work",
    )
    session.start()
    session.stop()
    assert session.work_dir.exists()  # segmenty są przed finalize

    engine.finalize_to_library(session, title="Nagranie", output_dir=tmp_path / "out")

    # Plik wynikowy zapisany, a redundantne _work usunięte; folder materiału i wpis zostają.
    assert (tmp_path / "out" / "Nagranie" / "Nagranie.mkv").is_file()
    assert not session.work_dir.exists()
    assert len(store.list_recordings(RecordingStatus.RECORDED)) == 1


# ── M17: duration z pliku wynikowego (ffprobe), nie z wallclocka legów ─────────


def _fake_probe(value: str, captured: list[list[str]]) -> Callable[[list[str]], str]:
    def runner(command: list[str]) -> str:
        captured.append(command)
        return value

    return runner


def _finalized_session(tmp_path: Path, probe_runner: Callable[[list[str]], str]) -> RecorderEngine:
    """Silnik z atrapą procesu/concat/probe; zwraca engine po skonstruowaniu (bez finalize)."""
    return RecorderEngine(
        encoders=_ENCODERS,
        process_factory=_fake_factory([]),
        concat_runner=_fake_concat([]),
        probe_runner=probe_runner,
    )


def test_finalize_duration_from_ffprobe(tmp_path: Path) -> None:
    """duration = długość z ffprobe pliku wynikowego (17.34 → 17.3), nie wallclock legów."""
    captured: list[list[str]] = []
    engine = _finalized_session(tmp_path, _fake_probe("17.34\n", captured))
    out = tmp_path / "out"
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=out / "Nagranie" / "_work",
    )
    session.start()
    session.stop()
    engine.finalize_to_library(session, title="Nagranie", output_dir=out)

    output_file = out / "Nagranie" / "Nagranie.mkv"
    assert read_metadata(out / "Nagranie").duration == 17.3
    assert captured == [build_probe_duration_command(output_file)]  # komenda z buildera


def test_finalize_duration_fallback_when_probe_empty(tmp_path: Path) -> None:
    """ffprobe zwraca śmieć/"" → fallback na wallclock (round(elapsed_seconds, 1))."""
    engine = _finalized_session(tmp_path, lambda _cmd: "")
    out = tmp_path / "out"
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=out / "Nagranie" / "_work",
    )
    session.start()
    session.stop()
    engine.finalize_to_library(session, title="Nagranie", output_dir=out)

    assert read_metadata(out / "Nagranie").duration == round(session.elapsed_seconds, 1)


def test_finalize_duration_fallback_when_probe_raises(tmp_path: Path) -> None:
    """Runner ffprobe rzuca → finalizacja NIE pada (plik już jest); fallback na wallclock."""

    def boom(_cmd: list[str]) -> str:
        raise OSError("ffprobe niedostępny")

    engine = _finalized_session(tmp_path, boom)
    out = tmp_path / "out"
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=out / "Nagranie" / "_work",
    )
    session.start()
    session.stop()
    engine.finalize_to_library(session, title="Nagranie", output_dir=out)  # nie rzuca

    assert read_metadata(out / "Nagranie").duration == round(session.elapsed_seconds, 1)


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


def test_acquire_twice_does_not_reuse_folder(tmp_path: Path) -> None:
    """Dwa kolejne acquire do tego samego output_dir → DWA foldery; plik pierwszego przeżywa drugi.

    Regresja na utratę danych: dawniej work_dir == folder materiału i stała nazwa „nagranie", więc
    drugie nagranie nadpisywało pierwsze. Teraz work_dir_for → podkatalog _work + next_free_title.
    """
    db_path = tmp_path / "library.sqlite3"
    Database(db_path).migrate()
    store = RecordingStore(db_path)
    out = tmp_path / "out"
    engine = RecorderEngine(
        encoders=_ENCODERS,
        store=store,
        process_factory=_fake_factory([]),
        concat_runner=_fake_concat([]),
    )
    opts = AcquireOptions(quality=PRESETS["standard"], output_dir=out)
    src = EngineSource(kind=SourceKind.SCREEN, target="desktop")

    engine.acquire(src, opts, lambda _f, _m: None, max_seconds=0.0)
    first = out / "nagranie" / "nagranie.mkv"
    assert first.is_file() and first.read_bytes() == b"FINAL"

    engine.acquire(src, opts, lambda _f, _m: None, max_seconds=0.0)
    second = out / "nagranie (2)" / "nagranie (2).mkv"
    assert second.is_file()
    # KLUCZOWE: drugie nagranie NIE nadpisało pierwszego — osobne foldery, plik pierwszego trwa.
    assert first.is_file() and first.read_bytes() == b"FINAL"
    assert len(store.list_recordings(RecordingStatus.RECORDED)) == 2


def test_engine_can_handle_only_screen() -> None:
    engine = RecorderEngine(encoders=_ENCODERS)
    assert engine.can_handle(EngineSource(kind=SourceKind.SCREEN, target="desktop")) is True
    assert engine.can_handle(EngineSource(kind=SourceKind.LOCAL_FILE, target="a.mp4")) is False
    assert len(engine.probe(EngineSource(kind=SourceKind.SCREEN, target="desktop"))) == 5


# ── Kolizja nazwy: czyszczenie katalogu roboczego + nadpisanie + wolna nazwa ──


def test_start_clears_stale_segments(tmp_path: Path) -> None:
    # Stare segmenty w katalogu roboczym skleiłyby się z nowymi (zmieszane sesje) — start czyści.
    work = tmp_path / "work"
    work.mkdir()
    (work / "seg_000.mkv").write_bytes(b"OLD")
    (work / "seg_001.mkv").write_bytes(b"OLD")
    spawned: list[list[str]] = []
    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=work,
        encoders=_ENCODERS,
        process_factory=_fake_factory(spawned),
        clock=_Clock(),
    )
    session.start()
    session.stop()

    names = sorted(p.name for p in segments.list_segments(work))
    assert names == ["seg_000.mkv"]  # stare usunięte, tylko nowy odcinek
    assert (work / "seg_000.mkv").read_bytes() == b"SEGMENT"  # świeża treść, nie OLD


def test_resume_keeps_segments(tmp_path: Path) -> None:
    # Regresja: wznowienie NIE czyści (kontynuuje numerację) — czyści tylko świadomy start.
    spawned: list[list[str]] = []
    session = RecorderSession(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=tmp_path / "work",
        encoders=_ENCODERS,
        process_factory=_fake_factory(spawned),
        clock=_Clock(),
    )
    session.start()
    session.pause()
    session.resume()
    session.stop()
    names = sorted(p.name for p in segments.list_segments(tmp_path / "work"))
    assert names == ["seg_000.mkv", "seg_001.mkv"]  # oba odcinki zachowane


def _seed_material_dir(output_dir: Path, title: str) -> Path:
    d = material_dir_for(output_dir, title)
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text("{}", encoding="utf-8")
    return d


def test_next_free_title_increments(tmp_path: Path) -> None:
    assert next_free_title(tmp_path, "Wykład") == "Wykład"  # wolna → sama
    _seed_material_dir(tmp_path, "Wykład")
    assert next_free_title(tmp_path, "Wykład") == "Wykład (2)"
    _seed_material_dir(tmp_path, "Wykład (2)")
    assert next_free_title(tmp_path, "Wykład") == "Wykład (3)"


def test_overwrite_replaces_material_and_resets_transcript(tmp_path: Path) -> None:
    db = tmp_path / "lib.sqlite3"
    Database(db).migrate()
    store = RecordingStore(db)
    lib = tmp_path / "lib"

    # Istniejący materiał z gotowym transkryptem (plik + wpis SQLite).
    mat = material_dir_for(lib, "Wykład")
    mat.mkdir(parents=True)
    old = MaterialMetadata(
        title="Wykład",
        created_at="t",
        video_path="Wykład.mkv",
        transcript_status="done",
        transcript_json="Wykład.json",
    )
    write_metadata(mat, old)
    (mat / "Wykład.json").write_text("{}", encoding="utf-8")
    (mat / "Wykład.mkv").write_bytes(b"OLD")
    store.upsert_material(mat, old)
    assert len(store.list_materials()) == 1

    # Nadpisanie: usuń folder, potem finalize świeżej sesji pod tą samą nazwą.
    discard_material_dir(lib, "Wykład")
    assert not mat.exists()

    spawned: list[list[str]] = []
    out: list[Path] = []
    engine = RecorderEngine(
        encoders=_ENCODERS,
        store=store,
        process_factory=_fake_factory(spawned),
        concat_runner=_fake_concat(out),
    )
    session = engine.new_session(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        work_dir=mat / "_work",
    )
    session.start()
    session.stop()
    engine.finalize_to_library(session, title="Wykład", output_dir=lib)

    mats = store.list_materials()
    assert len(mats) == 1  # bez duplikatu (upsert wg folderu)
    meta = mats[0][2]
    assert meta.transcript_status == "none" and meta.transcript_json is None
    assert not (mat / "Wykład.json").exists()  # stary transkrypt zniknął z folderu
