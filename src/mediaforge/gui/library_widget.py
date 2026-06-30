"""Widok biblioteki: lista materiałów + filtry (tag/kategoria) + edycja metadanych + podgląd.

Czyta materiały z SQLite (indeks), a edycja zapisuje ``metadata.json`` (źródło prawdy)
i synchronizuje z SQLite (:meth:`RecordingStore.upsert_material`). Podgląd = miniatura
z folderu materiału + „Otwórz folder". Bez własnych widgetów listy plików/logu.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore

_ALL = "(wszystkie)"


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


class LibraryWidget(QWidget):
    """Lista materiałów z metadanymi: filtrowanie, edycja, podgląd miniatury."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        Database(cfg_mod.library_db_path()).migrate()
        self._store = RecordingStore(cfg_mod.library_db_path())
        self._materials: list[tuple[int, Path, MaterialMetadata]] = []
        self._current: tuple[int, Path, MaterialMetadata] | None = None
        self._build_ui()
        self.refresh_all()

    # ── Budowa UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self._import_btn = QPushButton("Importuj…")
        self._import_btn.setToolTip("Zaimportuj lokalne pliki A/V do biblioteki")
        self._import_btn.clicked.connect(self._open_import)
        bar.addWidget(self._import_btn)
        bar.addStretch(1)
        bar.addWidget(QLabel("Kategoria:"))
        self._cat_filter = QComboBox()
        self._cat_filter.currentIndexChanged.connect(self.reload)
        bar.addWidget(self._cat_filter)
        bar.addWidget(QLabel("Tag:"))
        self._tag_filter = QComboBox()
        self._tag_filter.currentIndexChanged.connect(self.reload)
        bar.addWidget(self._tag_filter)
        root.addLayout(bar)

        splitter = QSplitter()
        self._list = QListWidget()
        self._list.setMinimumWidth(220)
        self._list.currentRowChanged.connect(self._on_select)
        splitter.addWidget(self._list)
        splitter.addWidget(self._build_details())
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

    def _build_details(self) -> QWidget:
        panel = QWidget()
        col = QVBoxLayout(panel)

        self._thumb = QLabel("(brak podglądu)")
        self._thumb.setMinimumHeight(150)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._thumb)

        form = QFormLayout()
        self._title = QLineEdit()
        form.addRow("Tytuł:", self._title)
        self._presenter = QLineEdit()
        form.addRow("Prowadzący:", self._presenter)
        self._organizer = QLineEdit()
        form.addRow("Organizator:", self._organizer)
        self._category = QLineEdit()
        form.addRow("Kategoria:", self._category)
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("tagi po przecinku")
        form.addRow("Tagi:", self._tags)
        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setEnabled(False)
        form.addRow("Info:", self._info)
        col.addLayout(form)

        actions = QHBoxLayout()
        self._save_btn = QPushButton("Zapisz metadane")
        self._save_btn.clicked.connect(self._on_save)
        actions.addWidget(self._save_btn)
        self._open_btn = QPushButton("Otwórz folder")
        self._open_btn.clicked.connect(self._open_folder)
        actions.addWidget(self._open_btn)
        actions.addStretch(1)
        col.addLayout(actions)
        col.addStretch(1)
        self._set_details_enabled(False)
        return panel

    # ── Dane ──────────────────────────────────────────────────────────────────

    def refresh_all(self) -> None:
        """Odświeża opcje filtrów (z bazy) i listę — po imporcie/edycji."""
        self._refresh_filters()
        self.reload()

    def _refresh_filters(self) -> None:
        for combo, values in (
            (self._cat_filter, self._store.all_categories()),
            (self._tag_filter, self._store.all_tags()),
        ):
            previous = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(_ALL)
            combo.addItems(values)
            idx = combo.findText(previous)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def reload(self) -> None:
        """Wczytuje materiały wg aktualnych filtrów do listy."""
        category = self._filter_value(self._cat_filter)
        tag = self._filter_value(self._tag_filter)
        self._materials = self._store.list_materials(tag=tag, category=category)
        self._list.blockSignals(True)
        self._list.clear()
        for _id, _folder, meta in self._materials:
            self._list.addItem(self._item_label(meta))
        self._list.blockSignals(False)
        if self._materials:
            self._list.setCurrentRow(0)
        else:
            self._current = None
            self._clear_details()

    @staticmethod
    def _filter_value(combo: QComboBox) -> str | None:
        text = combo.currentText()
        return None if combo.currentIndex() <= 0 or text == _ALL else text

    @staticmethod
    def _item_label(meta: MaterialMetadata) -> str:
        date = meta.created_at[:10] if meta.created_at else "—"
        return f"{meta.title}  ·  {date}  ·  {_fmt_duration(meta.duration)}"

    # ── Wybór / podgląd ───────────────────────────────────────────────────────

    def _on_select(self, row: int) -> None:
        if not (0 <= row < len(self._materials)):
            self._current = None
            self._clear_details()
            return
        self._current = self._materials[row]
        _id, folder, meta = self._current
        self._title.setText(meta.title)
        self._presenter.setText(meta.presenter or "")
        self._organizer.setText(meta.organizer or "")
        self._category.setText(meta.category or "")
        self._tags.setText(", ".join(meta.tags))
        self._info.setText(
            f"Data: {meta.created_at[:19] or '—'}  ·  Długość: {_fmt_duration(meta.duration)}  ·  "
            f"Źródło: {meta.source_type}  ·  Transkrypcja: {meta.transcript_status}  ·  "
            f"Streszczenie: {meta.summary_status}"
        )
        self._set_details_enabled(True)
        self._show_thumbnail(folder, meta)

    def _show_thumbnail(self, folder: Path, meta: MaterialMetadata) -> None:
        if meta.thumbnail_path:
            pixmap = QPixmap(str(folder / meta.thumbnail_path))
            if not pixmap.isNull():
                self._thumb.setPixmap(
                    pixmap.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation)
                )
                return
        self._thumb.setText("(brak podglądu)")

    # ── Edycja ────────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        if self._current is None:
            return
        _id, folder, meta = self._current
        updated = dataclasses.replace(
            meta,
            title=self._title.text().strip() or meta.title,
            presenter=self._presenter.text().strip() or None,
            organizer=self._organizer.text().strip() or None,
            category=self._category.text().strip() or None,
            tags=[t.strip() for t in self._tags.text().split(",") if t.strip()],
        )
        write_metadata(folder, updated)  # metadata.json = źródło prawdy
        self._store.upsert_material(folder, updated)  # synchronizacja indeksu
        self.refresh_all()

    def _open_folder(self) -> None:
        if self._current is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._current[1])))

    # ── Pomocnicze ────────────────────────────────────────────────────────────

    def _open_import(self) -> None:
        from mediaforge.gui.import_dialog import ImportDialog

        dialog = ImportDialog(self)
        dialog.exec()
        if dialog.imported_count:
            self.refresh_all()

    def _set_details_enabled(self, enabled: bool) -> None:
        for widget in (
            self._title,
            self._presenter,
            self._organizer,
            self._category,
            self._tags,
            self._save_btn,
            self._open_btn,
        ):
            widget.setEnabled(enabled)

    def _clear_details(self) -> None:
        for edit in (self._title, self._presenter, self._organizer, self._category, self._tags):
            edit.clear()
        self._info.clear()
        self._thumb.setText("(brak podglądu)")
        self._set_details_enabled(False)
