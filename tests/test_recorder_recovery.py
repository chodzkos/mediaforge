"""Test odporności nagrywania: symulacja przerwania → odzysk z segmentów.

Segmentacja crash-safe znaczy: nagłe zamknięcie gubi co najwyżej bieżący, niedokończony
segment; wcześniejsze (niepuste) są kompletne i dają się skleić w jeden plik.
"""

from __future__ import annotations

from pathlib import Path

from mediaforge.core.engines import segments


def _make_segment(work_dir: Path, index: int, *, data: bytes = b"DATA") -> Path:
    path = work_dir / f"{segments.SEGMENT_PREFIX}{index:03d}.mkv"
    path.write_bytes(data)
    return path


def test_partial_recording_recovers_completed_segments(tmp_path: Path) -> None:
    """Dwa ukończone segmenty + jeden pusty (przerwanie) → odzyskiwalne dwa pierwsze."""
    _make_segment(tmp_path, 0)
    _make_segment(tmp_path, 1)
    interrupted = _make_segment(tmp_path, 2, data=b"")  # segment otwarty w chwili crashu

    result = segments.plan_recovery(tmp_path, tmp_path / "final.mkv")

    assert result.recoverable is True
    assert [p.name for p in result.segments] == ["seg_000.mkv", "seg_001.mkv"]
    assert interrupted in result.dropped
    assert result.list_path is not None and result.list_path.exists()


def test_concat_list_lists_valid_segments(tmp_path: Path) -> None:
    _make_segment(tmp_path, 0)
    _make_segment(tmp_path, 1)
    result = segments.plan_recovery(tmp_path, tmp_path / "final.mkv")

    assert result.list_path is not None
    listing = result.list_path.read_text(encoding="utf-8")
    assert listing.count("file '") == 2
    assert "seg_000.mkv" in listing and "seg_001.mkv" in listing


def test_concat_command_copies_streams(tmp_path: Path) -> None:
    _make_segment(tmp_path, 0)
    result = segments.plan_recovery(tmp_path, tmp_path / "final.mkv", ffmpeg="ffmpeg")
    cmd = result.command
    assert cmd[0] == "ffmpeg"
    assert "concat" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"  # bez transkodowania
    assert cmd[-1] == str(tmp_path / "final.mkv")


def test_nothing_to_recover_when_no_segments(tmp_path: Path) -> None:
    result = segments.plan_recovery(tmp_path, tmp_path / "final.mkv")
    assert result.recoverable is False
    assert result.segments == []
    assert result.command == []


def test_only_empty_segments_not_recoverable(tmp_path: Path) -> None:
    _make_segment(tmp_path, 0, data=b"")
    result = segments.plan_recovery(tmp_path, tmp_path / "final.mkv")
    assert result.recoverable is False
