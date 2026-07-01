"""Widok biblioteki: lista materiałów + filtry + edycja metadanych + podgląd + transkrypcja.

Czyta materiały z SQLite (indeks), a edycja zapisuje ``metadata.json`` (źródło prawdy)
i synchronizuje z SQLite (:meth:`RecordingStore.upsert_material`). Import i transkrypcja
idą przez kolejkę ``jobs`` (wątek roboczy, GPU serializowane) — GUI tylko **odpytuje**
statusy ``QTimer``-em i streamuje je do ``LogView`` (bez sygnałów z wątków roboczych).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from chodzkos_gui_kit.qt.widgets import LogView
from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.ai.transcribe import WhisperCppBackend
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs import JobQueue, JobStatus, JobStore
from mediaforge.core.jobs.handlers import (
    DEFAULT_LANES,
    DEFAULT_ROUTES,
    JOB_IMPORT,
    JOB_TRANSCRIBE,
    make_import_handler,
    make_transcribe_handler,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore

_ALL = "(wszystkie)"
# Kolory statusów zadań w LogView (role palety — przeżywają zmianę motywu).
_JOB_LEVEL_COLORS = {"running": "accent2", "done": "accent", "error": "red", "queued": "fg2"}


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


class LibraryWidget(QWidget):
    """Lista materiałów z metadanymi: filtrowanie, edycja, podgląd miniatury."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        db_path = cfg_mod.library_db_path()
        Database(db_path).migrate()
        self._config = cfg_mod.load()
        self._store = RecordingStore(db_path)
        self._materials: list[tuple[int, Path, MaterialMetadata]] = []
        self._current: tuple[int, Path, MaterialMetadata] | None = None

        # Kolejka: import (linia I/O) + transkrypcja (linia GPU max_workers=1). Handlery
        # rejestrujemy teraz, ale wątek roboczy startuje dopiero start_jobs() (testy go nie
        # uruchamiają — sprawdzają samo kolejkowanie).
        self._jobs_store = JobStore(db_path)
        self._queue = JobQueue(self._jobs_store, lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
        self._queue.register(JOB_IMPORT, make_import_handler(ImporterEngine(store=self._store)))
        self._queue.register(JOB_TRANSCRIBE, make_transcribe_handler(self._store, self._backend()))
        self._seen: dict[int, str] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(800)
        self._poll.timeout.connect(self._poll_jobs)

        self._build_ui()
        self.refresh_all()

    def _backend(self) -> WhisperCppBackend:
        """Backend whisper.cpp z configu (binarka/model/wątki) dla handlera transkrypcji."""
        return WhisperCppBackend(
            model=cfg_mod.get_whisper_model(self._config) or "",
            whisper_cli=cfg_mod.get_whispercpp_path(self._config) or "whisper-cli",
            threads=cfg_mod.get_whisper_threads(self._config),
        )

    # ── Cykl życia kolejki (start z okna głównego; nie w testach) ─────────────

    def start_jobs(self) -> None:
        """Uruchamia wątek roboczy kolejki i polling statusów (woła okno główne)."""
        self._queue.start()
        self._poll.start()

    def shutdown(self) -> None:
        """Zatrzymuje polling i wątek roboczy (woła closeEvent okna głównego)."""
        self._poll.stop()
        self._queue.stop()

    # ── Budowa UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self._import_btn = QPushButton("Importuj…")
        self._import_btn.setToolTip("Zaimportuj lokalne pliki A/V do biblioteki")
        self._import_btn.clicked.connect(self._open_import)
        bar.addWidget(self._import_btn)
        self._rescan_btn = QPushButton("Przeskanuj")
        self._rescan_btn.setToolTip(
            "Odbuduj indeks z metadata.json w folderach biblioteki "
            "(po skasowaniu bazy / przeniesieniu biblioteki / ręcznej edycji)"
        )
        self._rescan_btn.clicked.connect(self._on_rescan)
        bar.addWidget(self._rescan_btn)
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
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_menu)
        delete_sc = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self._list)
        delete_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_sc.activated.connect(self._on_delete)
        splitter.addWidget(self._list)
        splitter.addWidget(self._build_details())
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        # Procent aktywnej transkrypcji (linia GPU serializuje → co najwyżej jedna naraz).
        self._job_status = QLabel("")
        self._job_status.setEnabled(False)
        root.addWidget(self._job_status)

        # Strumień statusów zadań (import/transkrypcja) — zasilany pollingiem QTimer.
        self._log = LogView(timestamps=True, level_colors=_JOB_LEVEL_COLORS)
        self._log.setMinimumHeight(110)
        self._log.setToolTip("Status zadań (import, transkrypcja)")
        root.addWidget(self._log)

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
        col.addLayout(form)

        # „Info" POZA formularzem: pełna szerokość + zawijanie pcha przyciski w dół, a nie
        # je przykrywa (w QFormLayout zawinięta 2. linia nachodziła na rząd akcji).
        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setEnabled(False)
        self._info.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        col.addWidget(self._info)

        actions = QHBoxLayout()
        self._save_btn = QPushButton("Zapisz metadane")
        self._save_btn.clicked.connect(self._on_save)
        actions.addWidget(self._save_btn)
        self._transcribe_btn = QPushButton("Transkrybuj")
        self._transcribe_btn.setToolTip("Dodaj transkrypcję (whisper.cpp) do kolejki")
        self._transcribe_btn.clicked.connect(self._on_transcribe)
        actions.addWidget(self._transcribe_btn)
        self._open_btn = QPushButton("Otwórz folder")
        self._open_btn.clicked.connect(self._open_folder)
        actions.addWidget(self._open_btn)
        actions.addStretch(1)
        self._delete_btn = QPushButton("Usuń")
        self._delete_btn.setToolTip("Trwale usuń materiał (nagranie + transkrypt + metadane)")
        self._delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self._delete_btn)
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
        badge = "  ·  📝" if meta.transcript_status == "done" else ""
        return f"{meta.title}  ·  {date}  ·  {_fmt_duration(meta.duration)}{badge}"

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

    # ── Usuwanie ──────────────────────────────────────────────────────────────

    def _on_list_menu(self, pos: QPoint) -> None:
        """Menu kontekstowe listy — pozycja „Usuń" dla klikniętego materiału."""
        if self._list.itemAt(pos) is None:
            return
        menu = QMenu(self._list)
        menu.addAction("Usuń", self._on_delete)
        menu.exec(self._list.mapToGlobal(pos))

    def _confirm_delete(self, title: str) -> bool:
        """Potwierdzenie z konsekwencjami; domyślny fokus = Anuluj (bezpieczny). Seam do testów."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Usunąć materiał?")
        root = QVBoxLayout(dlg)
        label = QLabel(
            f"Usunąć materiał «{title}»?\n\nZostaną trwale usunięte: nagranie, transkrypt "
            "i metadane. Tej operacji nie można cofnąć."
        )
        label.setWordWrap(True)
        root.addWidget(label)
        buttons = QDialogButtonBox()
        delete_btn = buttons.addButton("Usuń", QDialogButtonBox.ButtonRole.DestructiveRole)
        cancel_btn = buttons.addButton("Anuluj", QDialogButtonBox.ButtonRole.RejectRole)
        delete_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()
        root.addWidget(buttons)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _on_delete(self) -> None:
        """Usuwa bieżący materiał (folder + indeks) po potwierdzeniu; guard jobów jest w core."""
        if self._current is None:
            return
        rec_id, _folder, meta = self._current
        if not self._confirm_delete(meta.title):
            return
        try:
            self._store.delete_material(rec_id, cfg_mod.default_recordings_dir())
        except (ValueError, OSError) as exc:  # guard/path-safety (ValueError) lub błąd rmtree
            self._log.append_line(f"Nie usunięto «{meta.title}»: {exc}", "error")
            return
        self._log.append_line(f"Usunięto «{meta.title}»", "done")
        self._current = None
        self._clear_details()
        self.refresh_all()

    # ── Pomocnicze ────────────────────────────────────────────────────────────

    def _open_import(self) -> None:
        from mediaforge.gui.import_dialog import ImportDialog

        dialog = ImportDialog(self)
        dialog.exec()
        if dialog.enqueued_count:
            self._log.append_line(f"Import: dodano {dialog.enqueued_count} do kolejki", "queued")

    def _on_rescan(self) -> None:
        """Odbudowuje indeks z folderów biblioteki (metadata.json = źródło prawdy)."""
        self._store.rescan(cfg_mod.default_recordings_dir())
        self.refresh_all()

    def _on_transcribe(self) -> None:
        """Kolejkuje transkrypcję bieżącego materiału (whisper.cpp na linii GPU)."""
        if self._current is None:
            return
        if not cfg_mod.get_whisper_model(self._config):
            self._log.append_line(
                "Ustaw whisper_model w configu (sprawdź `doctor`) — brak modelu whisper.cpp.",
                "error",
            )
            return
        rec_id, _folder, meta = self._current
        self._jobs_store.enqueue(JOB_TRANSCRIBE, recording_id=rec_id)
        self._log.append_line(f"Transkrypcja w kolejce: {meta.title}", "queued")

    # ── Polling statusów zadań (QTimer; bez sygnałów z wątków roboczych) ──────

    def _poll_jobs(self) -> None:
        """Odpytuje kolejkę; loguje przejścia statusów, pokazuje % transkrypcji, odświeża listę."""
        completed = False
        running_transcribe = None
        for job in self._jobs_store.list_jobs():
            if job.job_type == JOB_TRANSCRIBE and job.status is JobStatus.RUNNING:
                running_transcribe = job
            if self._seen.get(job.id) == job.status.value:
                continue
            self._seen[job.id] = job.status.value
            label = f"{job.job_type} #{job.id}"
            if job.status is JobStatus.RUNNING:
                self._log.append_line(f"{label}: w toku…", "running")
            elif job.status is JobStatus.DONE:
                self._log.append_line(f"{label}: gotowe", "done")
                completed = True
            elif job.status is JobStatus.FAILED:
                self._log.append_line(f"{label}: błąd — {job.error_message or ''}", "error")
                completed = True
        # Procent zamiast samego „running" — żeby długi wykład nie wyglądał na zawieszony.
        if running_transcribe is not None:
            self._job_status.setText(f"Transkrypcja… {int(running_transcribe.progress * 100)}%")
        else:
            self._job_status.setText("")
        if completed:
            self.refresh_all()

    def _set_details_enabled(self, enabled: bool) -> None:
        for widget in (
            self._title,
            self._presenter,
            self._organizer,
            self._category,
            self._tags,
            self._save_btn,
            self._transcribe_btn,
            self._open_btn,
            self._delete_btn,
        ):
            widget.setEnabled(enabled)

    def _clear_details(self) -> None:
        for edit in (self._title, self._presenter, self._organizer, self._category, self._tags):
            edit.clear()
        self._info.clear()
        self._thumb.setText("(brak podglądu)")
        self._set_details_enabled(False)
