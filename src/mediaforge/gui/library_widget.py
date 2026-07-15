"""Widok biblioteki: lista materiałów + filtry + edycja metadanych + podgląd + transkrypcja.

Czyta materiały z SQLite (indeks), a edycja zapisuje ``metadata.json`` (źródło prawdy)
i synchronizuje z SQLite (:meth:`RecordingStore.upsert_material`). Import i transkrypcja
idą przez kolejkę ``jobs`` (wątek roboczy, GPU serializowane) — GUI tylko **odpytuje**
statusy ``QTimer``-em i streamuje je do ``LogView`` (bez sygnałów z wątków roboczych).
"""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.qt.widgets import LogView, make_scrollable
from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core import secrets
from mediaforge.core.ai.routing import resolve_route
from mediaforge.core.ai.summarize import (
    SummaryClient,
    SummaryConfig,
    read_transcript_text,
    summary_start_line,
)
from mediaforge.core.ai.transcribe import WhisperCppBackend
from mediaforge.core.ai.vision import VisionClient, VisionConfig
from mediaforge.core.detection import tools as detect_tools
from mediaforge.core.engines.download_engine import DownloaderEngine, run_ytdlp_update
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs import JobQueue, JobStatus, JobStore
from mediaforge.core.jobs.handlers import (
    DEFAULT_LANES,
    DEFAULT_ROUTES,
    JOB_DOWNLOAD,
    JOB_IMPORT,
    JOB_NOTES,
    JOB_SUMMARIZE,
    JOB_TRANSCRIBE,
    enqueue_notes,
    enqueue_summarize,
    make_download_handler,
    make_import_handler,
    make_notes_handler,
    make_summarize_handler,
    make_transcribe_handler,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.gui.material_details import MaterialDetailsPanel
from mediaforge.gui.settings_dialog import SettingsDialog

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
        self._queue.register(
            JOB_DOWNLOAD, make_download_handler(DownloaderEngine(store=self._store))
        )
        # JOB_TRANSCRIBE + JOB_SUMMARIZE + JOB_NOTES czytają config przy rejestracji — wołane tu
        # i po zapisie Ustawień, żeby zmiana modelu/ścieżek działała od następnego zadania.
        self.reload_ai_handlers()
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

    def reload_ai_handlers(self) -> None:
        """(Re)rejestruje handlery AI (transkrypcja + streszczenie + notatki) z configu i klientów.

        Wołane przy starcie i po zapisie Ustawień. ``JobQueue.register`` nadpisuje handler dla typu,
        więc następne zadanie użyje nowych modeli/gatewaya/limitów — bez restartu aplikacji (job już
        uruchomiony dokańcza się starym handlerem). Backend whisper (``_backend``) i klienci
        (``SummaryClient``/``VisionClient``) czytają config przy budowie, więc odświeżają
        binarkę/model whisper, base_url/timeout/limity — dlatego transkrypcja też jest tutaj.
        """
        self._queue.register(JOB_TRANSCRIBE, make_transcribe_handler(self._store, self._backend()))
        self._queue.register(
            JOB_SUMMARIZE,
            make_summarize_handler(
                self._store,
                self._summary_client(),
                local_model=cfg_mod.get_summary_model_local(self._config),
                cloud_model=cfg_mod.get_summary_model_cloud(self._config),
                chunk_chars=cfg_mod.get_summary_chunk_chars(self._config),
                reduce_max_tokens=cfg_mod.get_summary_reduce_max_tokens(self._config),
            ),
        )
        self._queue.register(
            JOB_NOTES,
            make_notes_handler(
                self._store,
                self._vision_client(),
                self._summary_client(),
                vlm_local=cfg_mod.get_vlm_model_local(self._config),
                vlm_cloud=cfg_mod.get_vlm_model_cloud(self._config),
                llm_local=cfg_mod.get_summary_model_local(self._config),
                llm_cloud=cfg_mod.get_summary_model_cloud(self._config),
            ),
        )

    def open_settings(self, parent: QWidget | None = None) -> None:
        """Otwiera dialog Ustawień AI; po zapisie re-rejestruje handlery (config od next joba)."""
        dialog = SettingsDialog(
            parent or self, config=self._config, on_saved=self.reload_ai_handlers
        )
        dialog.exec()

    def _summary_client(self) -> SummaryClient:
        """Klient gatewaya LiteLLM z configu — endpoint/język/limity + opcjonalny master key.

        Master key (jedyny sekret aplikacji) czytany z keyring, nie z configu/plaintext.
        """
        config = SummaryConfig(
            base_url=cfg_mod.get_litellm_base_url(self._config) or "http://localhost:4000",
            language=cfg_mod.get_summary_language(self._config),
            max_tokens=cfg_mod.get_summary_max_tokens(self._config),
            timeout=cfg_mod.get_summary_timeout(self._config),
            api_key=secrets.get_secret(secrets.GATEWAY_MASTER_KEY),
            prompt_suffix=cfg_mod.get_summary_prompt_suffix(self._config),
        )
        return SummaryClient(config)

    def _vision_client(self) -> VisionClient:
        """Klient VLM (analiza slajdów) z configu — endpoint/limit + opcjonalny master key.

        Timeout i sufiks współdzielone ze streszczeniami (``summary_timeout``/``prompt_suffix``);
        model wybiera routing (``vlm_model_*``). Master key z keyring (jedyny sekret aplikacji).
        """
        config = VisionConfig(
            base_url=cfg_mod.get_litellm_base_url(self._config) or "http://localhost:4000",
            max_tokens=cfg_mod.get_vlm_max_tokens(self._config),
            timeout=cfg_mod.get_summary_timeout(self._config),
            api_key=secrets.get_secret(secrets.GATEWAY_MASTER_KEY),
            prompt_suffix=cfg_mod.get_summary_prompt_suffix(self._config),
        )
        return VisionClient(config)

    # ── Cykl życia kolejki (start z okna głównego; nie w testach) ─────────────

    def start_jobs(self) -> None:
        """Uruchamia wątek roboczy kolejki i polling statusów (woła okno główne).

        Najpierw odzysk: zadania ``running`` przerwane poprzednim zamknięciem/awarią wracają
        do kolejki ZANIM ruszy dispatcher (inaczej zostałyby ``running`` na zawsze).
        """
        recovered = self._jobs_store.recover_stale()
        if recovered > 0:
            self._log.append_line(f"Przywrócono {recovered} przerwanych zadań do kolejki", "info")
        self._queue.start()
        self._poll.start()

    def shutdown(self) -> bool:
        """Zatrzymuje polling i wątek roboczy (woła closeEvent okna głównego).

        Zwraca ``False``, gdy zadania w toku nie domknęły się w limicie ``stop()`` — wtedy okno
        główne wpisuje ostrzeżenie (zadania odzyskają się przy następnym starcie).
        """
        self._poll.stop()
        return self._queue.stop()

    # ── Budowa UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self._import_btn = QPushButton("Importuj…")
        self._import_btn.setToolTip("Zaimportuj lokalne pliki A/V do biblioteki")
        self._import_btn.clicked.connect(self._open_import)
        bar.addWidget(self._import_btn)
        self._download_btn = QPushButton("Pobierz…")
        self._download_btn.setToolTip("Pobierz z URL przez yt-dlp (wideo/audio, bez DRM)")
        self._download_btn.clicked.connect(self._open_download)
        bar.addWidget(self._download_btn)
        self._podcast_btn = QPushButton("Podcast…")
        self._podcast_btn.setToolTip("Pobierz odcinki z kanału RSS podcastu")
        self._podcast_btn.clicked.connect(self._open_podcast)
        bar.addWidget(self._podcast_btn)
        self._profiles_btn = QPushButton("Profile…")
        self._profiles_btn.setToolTip("Edytuj profile źródeł (domyślne metadane per domena)")
        self._profiles_btn.clicked.connect(self._open_profiles)
        bar.addWidget(self._profiles_btn)
        self._update_ytdlp_btn = QPushButton("Aktualizuj yt-dlp")
        self._update_ytdlp_btn.setToolTip("Zaktualizuj yt-dlp (binarka: -U; moduł: instrukcja uv)")
        self._update_ytdlp_btn.clicked.connect(self._on_update_ytdlp)
        bar.addWidget(self._update_ytdlp_btn)
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
        """Buduje panel szczegółów (wydzielona klasa) i wiąże jego przyciski z akcjami widoku.

        Panel jest odsprzężony (nie zna store/kolejki/configu) — tu podłączamy sygnały
        ``clicked`` do handlerów biblioteki i czytamy edycje przez ``apply_edits``.
        """
        self._details = MaterialDetailsPanel()
        self._details.save_btn.clicked.connect(self._on_save)
        self._details.transcribe_btn.clicked.connect(self._on_transcribe)
        self._details.summarize_btn.clicked.connect(self._on_summarize)
        self._details.notes_btn.clicked.connect(self._on_notes)
        self._details.attach_slides_btn.clicked.connect(self._on_attach_slides)
        self._details.open_btn.clicked.connect(self._open_folder)
        self._details.delete_btn.clicked.connect(self._on_delete)
        # Panel to czysta treść — scroll przy niskim oknie daje kitowy make_scrollable.
        return make_scrollable(self._details)

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
            self._details.clear()

    @staticmethod
    def _filter_value(combo: QComboBox) -> str | None:
        text = combo.currentText()
        return None if combo.currentIndex() <= 0 or text == _ALL else text

    @staticmethod
    def _item_label(meta: MaterialMetadata) -> str:
        date = meta.created_at[:10] if meta.created_at else "—"
        badge = "  ·  📝" if meta.transcript_status == "done" else ""
        badge += "  ·  🧾" if meta.summary_status == "done" else ""
        badge += "  ·  🗒️" if meta.notes_status == "done" else ""
        return f"{meta.title}  ·  {date}  ·  {_fmt_duration(meta.duration)}{badge}"

    # ── Wybór / podgląd ───────────────────────────────────────────────────────

    def _on_select(self, row: int) -> None:
        if not (0 <= row < len(self._materials)):
            self._current = None
            self._details.clear()
            return
        self._current = self._materials[row]
        _id, folder, meta = self._current
        self._details.load(folder, meta)

    # ── Edycja ────────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        if self._current is None:
            return
        _id, folder, meta = self._current
        updated = self._details.apply_edits(meta)  # czyta pola panelu (bez IO)
        write_metadata(folder, updated)  # metadata.json = źródło prawdy
        self._store.upsert_material(folder, updated)  # synchronizacja indeksu
        self.refresh_all()

    def _on_attach_slides(self) -> None:
        """Wybór obrazów slajdów (pliki albo cały folder) → kopia do slides/ materiału.

        Kopiowanie/przeliczenie robi ``store.add_slides`` (nie-obrazy pomija). GUI tylko wybiera
        źródło i odświeża panel; slajdy z nazwą niosącą czas (np. mp.pl ``_450s``) mapują się same.
        """
        if self._current is None:
            return
        _rec_id, folder, meta = self._current
        files = self._pick_slide_sources()
        if not files:
            return
        try:
            self._store.add_slides(folder, meta, files)
        except OSError as exc:
            self._log.append_line(f"Nie podłączono slajdów: {exc}", "error")
            return
        self._log.append_line(f"Podłączono slajdy: {meta.title}", "done")
        self.refresh_all()

    def _pick_slide_sources(self) -> list[Path]:
        """Dialog wyboru obrazów: pliki, a gdy nic nie wybrano — cały folder (wszystkie obrazy).

        Seam do testów (pytest-qt nie klika natywnego dialogu) — podmieniany w testach.
        """
        picked, _filter = QFileDialog.getOpenFileNames(
            self, "Wybierz obrazy slajdów", "", "Obrazy (*.png *.jpg *.jpeg *.webp *.gif)"
        )
        if picked:
            return [Path(p) for p in picked]
        directory = QFileDialog.getExistingDirectory(self, "Albo wskaż folder ze slajdami")
        return sorted(Path(directory).iterdir()) if directory else []

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
        self._details.clear()
        self.refresh_all()

    # ── Pomocnicze ────────────────────────────────────────────────────────────

    def _open_import(self) -> None:
        from mediaforge.gui.import_dialog import ImportDialog

        dialog = ImportDialog(self)
        dialog.exec()
        if dialog.enqueued_count:
            self._log.append_line(f"Import: dodano {dialog.enqueued_count} do kolejki", "queued")

    def _open_download(self) -> None:
        from mediaforge.gui.download_dialog import DownloadDialog

        dialog = DownloadDialog(self)
        dialog.exec()
        if dialog.enqueued_count:
            self._log.append_line(
                f"Pobieranie: dodano {dialog.enqueued_count} do kolejki", "queued"
            )

    def _open_podcast(self) -> None:
        from mediaforge.gui.podcast_dialog import PodcastDialog

        dialog = PodcastDialog(self)
        dialog.exec()
        if dialog.enqueued_count:
            self._log.append_line(
                f"Podcast: dodano {dialog.enqueued_count} odcinków do kolejki", "queued"
            )

    def _open_profiles(self) -> None:
        from mediaforge.gui.profiles_dialog import ProfilesDialog

        ProfilesDialog(self).exec()

    def _on_update_ytdlp(self) -> None:
        """Aktualizuje yt-dlp wg wariantu z detekcji (binarka: -U; moduł: instrukcja uv)."""
        report = detect_tools.check_ytdlp()
        message = run_ytdlp_update(available=bool(report.get("available")), path=report.get("path"))
        for line in message.splitlines():
            self._log.append_line(line, "info")

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
                "Wskaż model whisper.cpp w Ustawieniach (ikona zębatki → Transkrypcja) — "
                "brak modelu.",
                "error",
            )
            return
        rec_id, _folder, meta = self._current
        self._jobs_store.enqueue(JOB_TRANSCRIBE, recording_id=rec_id)
        self._log.append_line(f"Transkrypcja w kolejce: {meta.title}", "queued")

    def _on_summarize(self) -> None:
        """Kolejkuje streszczenie bieżącego materiału (lokalnie/chmura wg cloud_ok i configu).

        Linia (GPU/IO) dobierana w :func:`enqueue_summarize` wg trasy; odmowa bez transkryptu.
        """
        if self._current is None:
            return
        if not cfg_mod.get_summary_model_local(self._config):
            self._log.append_line(
                "Ustaw model streszczeń w Ustawieniach (ikona zębatki w prawym górnym rogu).",
                "error",
            )
            return
        rec_id, folder, meta = self._current
        try:
            enqueue_summarize(
                self._store,
                self._jobs_store,
                rec_id,
                local_model=cfg_mod.get_summary_model_local(self._config),
                cloud_model=cfg_mod.get_summary_model_cloud(self._config),
            )
        except ValueError as exc:  # brak transkryptu / materiał nie istnieje
            self._log.append_line(f"Nie streszczono «{meta.title}»: {exc}", "error")
            return
        where = "chmura" if meta.cloud_ok else "lokalnie"
        self._log.append_line(f"Streszczanie w kolejce ({where}): {meta.title}", "queued")
        self._log_summary_input(folder, meta)

    def _on_notes(self) -> None:
        """Kolejkuje notatkę per slajd (VLM + komentarz) — odmowa bez slajdów/transkryptu.

        Linia (GPU/IO) dobierana w :func:`enqueue_notes` wg tras VLM/LLM (lokalna → GPU).
        """
        if self._current is None:
            return
        if not cfg_mod.get_vlm_model_local(self._config):
            self._log.append_line(
                "Ustaw model VLM (Notatki) w Ustawieniach (ikona zębatki w prawym górnym rogu).",
                "error",
            )
            return
        rec_id, _folder, meta = self._current
        try:
            enqueue_notes(
                self._store,
                self._jobs_store,
                rec_id,
                vlm_local=cfg_mod.get_vlm_model_local(self._config),
                vlm_cloud=cfg_mod.get_vlm_model_cloud(self._config),
                llm_local=cfg_mod.get_summary_model_local(self._config),
                llm_cloud=cfg_mod.get_summary_model_cloud(self._config),
            )
        except ValueError as exc:  # brak slajdów / transkryptu / materiał nie istnieje
            self._log.append_line(f"Nie utworzono notatki «{meta.title}»: {exc}", "error")
            return
        where = "chmura" if meta.cloud_ok else "lokalnie"
        self._log.append_line(f"Notatka w kolejce ({where}): {meta.title}", "queued")

    def _log_summary_input(self, folder: Path, meta: MaterialMetadata) -> None:
        """Loguje rozmiar wejścia streszczenia (znaki, model, timeout) do LogView.

        Job jest już w kolejce — błąd odczytu transkryptu nie może wywrócić akcji, więc linię
        po cichu pomijamy. Trasę (model) liczymy tak samo jak :func:`enqueue_summarize`.
        """
        if not meta.transcript_json:
            return
        try:
            text = read_transcript_text(folder / meta.transcript_json)
        except (OSError, ValueError):
            return
        route = resolve_route(
            cloud_ok=meta.cloud_ok,
            local_model=cfg_mod.get_summary_model_local(self._config),
            cloud_model=cfg_mod.get_summary_model_cloud(self._config),
        )
        timeout = cfg_mod.get_summary_timeout(self._config)
        self._log.append_line(summary_start_line(len(text), route.model, timeout), "running")

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
