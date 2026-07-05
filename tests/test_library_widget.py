"""Widok biblioteki (pytest-qt, offscreen): lista, filtr, edycja metadanych → trwałość."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.core.jobs import JobStore
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.gui.import_dialog import ImportDialog
from mediaforge.gui.library_widget import LibraryWidget


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    monkeypatch.setattr(cfg_mod, "library_db_path", lambda: db)
    monkeypatch.setattr(cfg_mod, "default_recordings_dir", lambda: tmp_path / "lib")
    Database(db).migrate()
    return db


def _seed(db: Path, tmp_path: Path, title: str, *, category: str, tags: list[str]) -> Path:
    folder = tmp_path / "lib" / title
    meta = MaterialMetadata(
        title=title,
        created_at="2026-06-30T10:00:00+00:00",
        category=category,
        tags=tags,
        duration=61.0,
    )
    write_metadata(folder, meta)
    RecordingStore(db).upsert_material(folder, meta)
    return folder


def test_library_lists_and_edits_persist(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Wykład", category="Sieci", tags=["tcp"])

    widget = LibraryWidget()
    qtbot.addWidget(widget)

    assert widget._list.count() == 1
    widget._list.setCurrentRow(0)
    assert widget._details.title.text() == "Wykład"

    # Edycja metadanych → zapis do metadata.json (źródło prawdy) + SQLite.
    widget._details.title.setText("Wykład o BGP")
    widget._details.presenter.setText("dr Nowak")
    widget._details.tags.setText("tcp, bgp")
    widget._on_save()

    saved = read_metadata(folder)
    assert saved.title == "Wykład o BGP"
    assert saved.presenter == "dr Nowak"
    assert saved.tags == ["bgp", "tcp"]
    # Indeks SQLite zsynchronizowany.
    assert RecordingStore(db).list_materials()[0][2] == saved


def test_library_filters_by_category(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    _seed(db, tmp_path, "A", category="Sieci", tags=["tcp"])
    _seed(db, tmp_path, "B", category="AI", tags=["llm"])

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    assert widget._list.count() == 2

    idx = widget._cat_filter.findText("AI")
    widget._cat_filter.setCurrentIndex(idx)
    assert widget._list.count() == 1


def test_transcribe_button_enqueues_job(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    _seed(db, tmp_path, "Wyklad", category="Sieci", tags=["tcp"])

    widget = LibraryWidget()  # start_jobs() NIE wołane → brak wątku roboczego
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    # Bez whisper_model → nie kolejkuje (log błędu).
    widget._on_transcribe()
    assert JobStore(db).list_jobs() == []

    # Z modelem → job transkrypcji w kolejce (recording_id materiału).
    cfg_mod.set_whisper_model(widget._config, "/m/model.bin")
    widget._on_transcribe()
    jobs = JobStore(db).list_jobs()
    assert len(jobs) == 1 and jobs[0].job_type == "transcribe"
    assert jobs[0].recording_id is not None


def test_attach_slides_button_copies_and_shows_gallery(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """„Podłącz slajdy" kopiuje obrazy do slides/, licznik w Info i galeria z timestampem."""
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Wyklad", category="Sieci", tags=["tcp"])
    src = tmp_path / "src"
    src.mkdir()
    for name in ("s_0s.png", "s_154s.png", "notatki.txt"):
        (src / name).write_bytes(b"X")

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)
    # Podmieniamy natywny dialog wyboru plików (pytest-qt go nie kliknie).
    monkeypatch.setattr(
        widget,
        "_pick_slide_sources",
        lambda: [src / "s_0s.png", src / "s_154s.png", src / "notatki.txt"],
    )
    widget._on_attach_slides()

    copied = sorted(p.name for p in (folder / "slides").iterdir())
    assert copied == ["s_0s.png", "s_154s.png"]  # nie-obraz pominięty
    assert read_metadata(folder).slides[1].timestamp_s == 154
    # Panel: licznik slajdów w Info + galeria z podpisem timestampu (2:34).
    assert "Slajdy: 2" in widget._details._info.text()
    assert widget._details._slides_gallery.count() == 2
    labels = {widget._details._slides_gallery.item(i).text() for i in range(2)}
    assert "2:34" in labels  # 154 s → 2:34 (widoczny sygnał mapy czasowej)


def test_import_dialog_constructs(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    dialog = ImportDialog()
    qtbot.addWidget(dialog)
    assert dialog.enqueued_count == 0
    # Bez plików import nie kolejkuje (loguje ostrzeżenie).
    dialog._on_import()
    assert dialog.enqueued_count == 0


# ── Usuwanie materiału z biblioteki ───────────────────────────────────────────


def test_delete_confirmed_removes_material(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Do usunięcia", category="X", tags=[])
    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    monkeypatch.setattr(widget, "_confirm_delete", lambda title: True)
    widget._on_delete()

    assert widget._list.count() == 0  # zniknął z listy
    assert not folder.exists()  # folder usunięty z dysku
    assert widget._current is None
    assert "Usunięto" in widget._log.toPlainText()


def test_delete_cancelled_keeps_material(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Zostaje", category="X", tags=[])
    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    monkeypatch.setattr(widget, "_confirm_delete", lambda title: False)  # Anuluj
    widget._on_delete()

    assert widget._list.count() == 1 and folder.exists()  # nic nie ruszone


def test_delete_with_active_job_shows_error(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "W transkrypcji", category="X", tags=[])
    rec_id = RecordingStore(db).list_materials()[0][0]
    JobStore(db).enqueue("transcribe", recording_id=rec_id)  # aktywny job (pending)

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    monkeypatch.setattr(widget, "_confirm_delete", lambda title: True)
    widget._on_delete()

    assert widget._list.count() == 1 and folder.exists()  # guard: nic nie usunięte
    assert "aktywne zadanie" in widget._log.toPlainText()
