"""Testy magazynu nagrań (tabela ``recordings``)."""

from __future__ import annotations

from pathlib import Path

from mediaforge.core.library.db import Database
from mediaforge.core.library.recordings import RecordingStatus, RecordingStore


def _store(tmp_path: Path) -> RecordingStore:
    db_path = tmp_path / "library.sqlite3"
    Database(db_path).migrate()
    return RecordingStore(db_path)


def test_create_recording_defaults_to_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec_id = store.create(
        "Wykład 1",
        duration=123.4,
        video_path=tmp_path / "wyklad.mkv",
    )
    rec = store.get(rec_id)
    assert rec is not None
    assert rec.title == "Wykład 1"
    assert rec.status is RecordingStatus.RECORDED
    assert rec.duration == 123.4
    assert rec.video_path is not None and rec.video_path.endswith("wyklad.mkv")


def test_set_status_updates_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec_id = store.create("X", status=RecordingStatus.RECORDING)
    store.set_status(rec_id, RecordingStatus.RECORDED)
    rec = store.get(rec_id)
    assert rec is not None
    assert rec.status is RecordingStatus.RECORDED


def test_list_filters_by_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create("a", status=RecordingStatus.RECORDED)
    store.create("b", status=RecordingStatus.FAILED)
    recorded = store.list_recordings(RecordingStatus.RECORDED)
    assert [r.title for r in recorded] == ["a"]
    assert len(store.list_recordings()) == 2
