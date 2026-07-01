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


# ── Usuwanie materiałów (folder + wiersz, guardy) ─────────────────────────────

import shutil  # noqa: E402

import pytest  # noqa: E402

from mediaforge.core.jobs.store import JobStore  # noqa: E402
from mediaforge.core.library.material import MaterialMetadata, write_metadata  # noqa: E402


def _seed_material(store: RecordingStore, lib: Path, title: str) -> tuple[int, Path]:
    folder = lib / title
    meta = MaterialMetadata(title=title, created_at="t", video_path=f"{title}.mkv")
    write_metadata(folder, meta)
    (folder / f"{title}.mkv").write_bytes(b"VIDEO")
    return store.upsert_material(folder, meta), folder


def test_delete_removes_folder_and_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lib = tmp_path / "lib"
    rec_id, folder = _seed_material(store, lib, "Wyklad")
    assert folder.exists() and len(store.list_materials()) == 1

    store.delete_material(rec_id, lib)

    assert not folder.exists()  # folder faktycznie znika z dysku
    assert store.list_materials() == []
    assert store.get_material(rec_id) is None


def test_delete_rmtree_failure_keeps_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    lib = tmp_path / "lib"
    rec_id, folder = _seed_material(store, lib, "Wyklad")

    def boom(_path: Path) -> None:
        raise OSError("plik zajęty przez odtwarzacz")

    monkeypatch.setattr(shutil, "rmtree", boom)
    with pytest.raises(OSError, match="zajęty"):
        store.delete_material(rec_id, lib)

    assert folder.exists()  # nic nie skasowane
    assert len(store.list_materials()) == 1  # wiersz zostaje (baza nie kłamie)


def test_delete_refused_with_active_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = JobStore(store.path)
    lib = tmp_path / "lib"
    rec_id, folder = _seed_material(store, lib, "Wyklad")
    jobs.enqueue("transcribe", recording_id=rec_id)  # pending

    with pytest.raises(ValueError, match="aktywne zadanie"):
        store.delete_material(rec_id, lib)
    assert folder.exists() and len(store.list_materials()) == 1

    jobs.claim_next()  # → running: nadal odmowa
    with pytest.raises(ValueError, match="aktywne zadanie"):
        store.delete_material(rec_id, lib)
    assert folder.exists()


def test_delete_allowed_with_finished_job_cascades(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = JobStore(store.path)
    lib = tmp_path / "lib"
    rec_id, folder = _seed_material(store, lib, "Wyklad")
    job_id = jobs.enqueue("transcribe", recording_id=rec_id)
    jobs.claim_next()
    jobs.mark_done(job_id)

    store.delete_material(rec_id, lib)

    assert not folder.exists() and store.list_materials() == []
    assert jobs.get(job_id) is None  # FK ON DELETE CASCADE posprzątał job


def test_delete_path_safety_refuses_outside_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lib = tmp_path / "lib"
    outside = tmp_path / "outside"  # folder POZA library_root
    meta = MaterialMetadata(title="Obcy", created_at="t")
    write_metadata(outside, meta)
    rec_id = store.upsert_material(outside, meta)

    with pytest.raises(ValueError, match="poza biblioteką"):
        store.delete_material(rec_id, lib)
    assert outside.exists() and len(store.list_materials()) == 1  # nic nie ruszone


def test_delete_then_rescan_does_not_return(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lib = tmp_path / "lib"
    rec_id, _folder = _seed_material(store, lib, "Wyklad")

    store.delete_material(rec_id, lib)
    store.rescan(lib)  # spójność architektury: brak folderu → materiał nie wraca

    assert store.list_materials() == []
