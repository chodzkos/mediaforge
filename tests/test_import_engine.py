"""ImporterEngine: budowa komend FFmpeg (czysta) + orkiestracja importu (z atrapami)."""

from __future__ import annotations

from pathlib import Path

from mediaforge.core.engines.base import AcquireOptions, Source, SourceKind
from mediaforge.core.engines.ffmpeg_cmd import PRESETS
from mediaforge.core.engines.import_engine import (
    ImporterEngine,
    build_extract_audio_command,
    build_probe_duration_command,
    build_thumbnail_command,
    is_supported,
    parse_duration,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import read_metadata
from mediaforge.core.library.recordings import RecordingStore

# ── Czyste buildery / parsery ─────────────────────────────────────────────────


def test_supported_extensions() -> None:
    assert is_supported(Path("a.mp4")) and is_supported(Path("a.MKV"))
    assert is_supported(Path("a.mp3")) and is_supported(Path("a.wav"))
    assert not is_supported(Path("a.txt"))


def test_extract_audio_command_strips_video() -> None:
    cmd = build_extract_audio_command(Path("in.mp4"), Path("out.m4a"))
    assert "-vn" in cmd and cmd[-1] == "out.m4a" and "in.mp4" in cmd


def test_thumbnail_command_single_frame_fast_seek() -> None:
    cmd = build_thumbnail_command(Path("in.mp4"), Path("t.jpg"), at_seconds=2.0)
    assert cmd.index("-ss") < cmd.index("-i")  # seek przed wejściem = szybciej
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"


def test_probe_duration_round_trip() -> None:
    cmd = build_probe_duration_command(Path("in.mp4"))
    assert "format=duration" in cmd
    assert parse_duration("3601.50\n") == 3601.5
    assert parse_duration("") is None
    assert parse_duration("not-a-number") is None


# ── Orkiestracja importu (atrapy runnera/kopiowania) ──────────────────────────


def _fake_copy(src: Path, dest: Path) -> object:
    dest.write_bytes(b"FAKE")  # symuluje skopiowany plik
    return dest


def _store(tmp_path: Path) -> RecordingStore:
    db_path = tmp_path / "library.sqlite3"
    Database(db_path).migrate()
    return RecordingStore(db_path)


def test_import_video_creates_folder_metadata_and_entry(tmp_path: Path) -> None:
    src = tmp_path / "Wykład.mp4"
    src.write_bytes(b"SRC")
    store = _store(tmp_path)
    commands: list[list[str]] = []
    engine = ImporterEngine(
        store=store,
        runner=lambda cmd: commands.append(cmd) or 0,  # type: ignore[func-returns-value]
        probe_runner=lambda cmd: "120.0\n",
        copy_fn=_fake_copy,
    )

    artifact = engine.import_file(
        src, tmp_path / "lib", lambda f, m: None, category="Sieci", tags=["tcp"]
    )

    material_dir = tmp_path / "lib" / "Wykład"
    assert (material_dir / "Wykład.mp4").exists()  # plik skopiowany
    meta = read_metadata(material_dir)  # metadata.json = źródło prawdy
    assert meta.title == "Wykład" and meta.category == "Sieci" and meta.tags == ["tcp"]
    assert meta.video_path == "Wykład.mp4" and meta.audio_path == "Wykład.m4a"
    assert meta.thumbnail_path == "thumbnail.jpg" and meta.duration == 120.0
    # Zsynchronizowane z SQLite.
    assert len(store.list_materials()) == 1
    assert store.list_materials()[0][2] == meta
    # Wywołano ekstrakcję audio i miniaturę.
    assert any("-vn" in c for c in commands) and any("-frames:v" in c for c in commands)
    assert artifact.video_path is not None


def test_import_audio_only_no_extraction(tmp_path: Path) -> None:
    src = tmp_path / "podcast.mp3"
    src.write_bytes(b"SRC")
    store = _store(tmp_path)
    engine = ImporterEngine(
        store=store,
        runner=lambda cmd: 0,
        probe_runner=lambda cmd: "60\n",
        copy_fn=_fake_copy,
    )

    engine.import_file(src, tmp_path / "lib", lambda f, m: None)

    meta = read_metadata(tmp_path / "lib" / "podcast")
    assert meta.video_path is None
    assert meta.audio_path == "podcast.mp3"  # samo audio = bez ekstrakcji
    assert meta.thumbnail_path is None


def test_engine_can_handle_only_supported_local_files() -> None:
    engine = ImporterEngine()
    assert engine.can_handle(Source(SourceKind.LOCAL_FILE, "x.mp4")) is True
    assert engine.can_handle(Source(SourceKind.LOCAL_FILE, "x.txt")) is False
    assert engine.can_handle(Source(SourceKind.SCREEN, "desktop")) is False


def test_acquire_uses_source_target(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"SRC")
    engine = ImporterEngine(
        runner=lambda cmd: 0, probe_runner=lambda cmd: "5\n", copy_fn=_fake_copy
    )
    artifact = engine.acquire(
        Source(SourceKind.LOCAL_FILE, str(src)),
        AcquireOptions(quality=PRESETS["standard"], output_dir=tmp_path / "lib"),
        lambda f, m: None,
    )
    assert artifact.video_path is not None
