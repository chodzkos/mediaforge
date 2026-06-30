"""Dialog importu lokalnych plików A/V — kolejkuje joby importu (nie blokuje UI).

Lista plików to kitowy ``FileList`` (drag&drop + toolbar Dodaj/Usuń/Wyczyść) — bez
własnej listy. Katalog docelowy biblioteki przez kitowy ``PathEntry``. Metadane wspólne
(kategoria, tagi) stosowane do wszystkich; szczegóły edytuje się potem w bibliotece.
Import idzie przez kolejkę ``jobs`` (kind ``import``) — kopia+FFmpeg w wątku roboczym,
więc UI się nie blokuje przy dużych plikach; postęp pokazuje biblioteka (polling).
"""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.qt.widgets import FileList, FileListTexts, LogView, PathEntry
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.engines.import_engine import SUPPORTED_EXTS
from mediaforge.core.jobs import JobStore
from mediaforge.core.jobs.handlers import JOB_IMPORT
from mediaforge.core.library.db import Database

_FILELIST_TEXTS = FileListTexts(
    files="Pliki",
    folder="Folder",
    remove="Usuń",
    clear="Wyczyść",
    tooltip_files="Dodaj pliki A/V do importu",
    tooltip_folder="Dodaj zawartość folderu",
    tooltip_remove="Usuń zaznaczone z listy",
    tooltip_clear="Wyczyść listę",
    list_tooltip="Przeciągnij tu pliki audio/wideo albo użyj przycisków",
    dialog_add_files="Wybierz pliki A/V",
    dialog_add_folder="Wybierz folder",
    filter_supported="Pliki A/V",
)


class ImportDialog(QDialog):
    """Wybór plików + wspólne metadane → joby importu w kolejce (kind ``import``)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import materiałów")
        self.setMinimumWidth(560)
        self.enqueued_count = 0

        Database(cfg_mod.library_db_path()).migrate()
        self._jobs = JobStore(cfg_mod.library_db_path())
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self._files = FileList(extensions=set(SUPPORTED_EXTS), texts=_FILELIST_TEXTS)
        root.addWidget(self._files, stretch=1)

        form = QFormLayout()
        self._dest = PathEntry(mode="dir", placeholder="Katalog biblioteki")
        self._dest.set(str(cfg_mod.default_recordings_dir()))
        form.addRow("Biblioteka:", self._dest)
        self._category = QLineEdit()
        self._category.setPlaceholderText("np. Konferencja 2026")
        form.addRow("Kategoria:", self._category)
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("tagi po przecinku")
        form.addRow("Tagi:", self._tags)
        root.addLayout(form)

        self._log = LogView(timestamps=True)
        self._log.setMinimumHeight(120)
        root.addWidget(self._log)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Importuj")
        ok.clicked.connect(self._on_import)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        root.addWidget(buttons)

    def _on_import(self) -> None:
        files = self._files.files()
        if not files:
            self._log.append_line("Najpierw dodaj pliki.", "warning")
            return
        library_root = str(self._dest.get() or str(cfg_mod.default_recordings_dir()))
        category = self._category.text().strip() or None
        tags = [t.strip() for t in self._tags.text().split(",") if t.strip()]

        for path in files:
            self._jobs.enqueue(
                JOB_IMPORT,
                payload={
                    "src": str(Path(path)),
                    "library_root": library_root,
                    "category": category,
                    "tags": tags,
                },
            )
            self._log.append_line(f"Dodano do kolejki importu: {Path(path).name}", "ok")
            self.enqueued_count += 1

        if self.enqueued_count:
            self.accept()
