"""Dialog pobierania URL (yt-dlp) — kolejkuje job pobrania (nie blokuje UI).

Metadane materiału mogą pochodzić z profilu źródła (per domena): po wpisaniu URL-a pola
kategoria/tagi/organizator i zgoda na chmurę są prefillowane z profilu, jeśli istnieje.
Zgoda na sesję przeglądarki (``cookies-from-browser``) jest OPT-IN — domyślnie wyłączona;
aplikacja nie widzi ani nie zapisuje poświadczeń (granica prawna, patrz CLAUDE.md).
"""

from __future__ import annotations

from chodzkos_gui_kit.qt.widgets import LogView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.engines.download_engine import COOKIE_BROWSERS, domain_of
from mediaforge.core.jobs import JobStore
from mediaforge.core.jobs.handlers import JOB_DOWNLOAD
from mediaforge.core.library.db import Database
from mediaforge.core.library.profiles import SourceProfile, SourceProfileStore


class DownloadDialog(QDialog):
    """URL + opcje/metadane → job pobrania w kolejce (kind ``download``)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pobierz z URL")
        self.setMinimumWidth(560)
        self.enqueued_count = 0

        db_path = cfg_mod.library_db_path()
        Database(db_path).migrate()
        self._jobs = JobStore(db_path)
        self._profiles = SourceProfileStore(db_path)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        form = QFormLayout()
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://… (wideo/audio; bez DRM)")
        self._url.editingFinished.connect(self._prefill_from_profile)
        form.addRow("URL:", self._url)

        self._audio_only = QCheckBox("Tylko audio (ekstrakcja ścieżki dźwiękowej)")
        form.addRow("", self._audio_only)

        # Cookies-from-browser — OPT-IN (domyślnie wyłączone). Jedyny tor zalogowany.
        cookies_row = QHBoxLayout()
        self._use_cookies = QCheckBox("Użyj sesji przeglądarki:")
        self._use_cookies.setToolTip(
            "Zalogowany dostęp przez sesję przeglądarki (yt-dlp --cookies-from-browser). "
            "Aplikacja nie widzi ani nie zapisuje haseł. Domyślnie wyłączone."
        )
        self._browser = QComboBox()
        self._browser.addItems(COOKIE_BROWSERS)
        self._browser.setEnabled(False)
        self._use_cookies.toggled.connect(self._browser.setEnabled)
        cookies_row.addWidget(self._use_cookies)
        cookies_row.addWidget(self._browser)
        cookies_row.addStretch(1)
        form.addRow("Logowanie:", cookies_row)

        self._title = QLineEdit()
        self._title.setPlaceholderText("(puste = tytuł z metadanych źródła)")
        form.addRow("Tytuł:", self._title)
        self._category = QLineEdit()
        form.addRow("Kategoria:", self._category)
        self._organizer = QLineEdit()
        form.addRow("Organizator:", self._organizer)
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("tagi po przecinku")
        form.addRow("Tagi:", self._tags)

        self._cloud_ok = QCheckBox("Zezwól na przetwarzanie w chmurze")
        self._cloud_ok.setToolTip("Bez zgody materiał jest przetwarzany wyłącznie lokalnie")
        form.addRow("Prywatność:", self._cloud_ok)
        self._save_profile = QCheckBox("Zapamiętaj te metadane jako profil domeny")
        form.addRow("", self._save_profile)
        root.addLayout(form)

        self._log = LogView(timestamps=True)
        self._log.setMinimumHeight(100)
        root.addWidget(self._log)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Pobierz")
        ok.clicked.connect(self._on_download)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        root.addWidget(buttons)

    def _prefill_from_profile(self) -> None:
        """Po wpisaniu URL-a: prefill metadanych z profilu domeny (jeśli istnieje)."""
        domain = domain_of(self._url.text().strip())
        if not domain:
            return
        profile = self._profiles.get(domain)
        # Profil istnieje → prefill i ukryj „zapisz profil"; brak → pokaż propozycję zapisu.
        self._save_profile.setVisible(profile is None)
        if profile is None:
            return
        if profile.category:
            self._category.setText(profile.category)
        if profile.organizer:
            self._organizer.setText(profile.organizer)
        if profile.tags:
            self._tags.setText(", ".join(profile.tags))
        self._cloud_ok.setChecked(
            profile.cloud_ok
        )  # domyślny cloud_ok z profilu (fail-safe: brak=False)

    def _on_download(self) -> None:
        url = self._url.text().strip()
        if not url:
            self._log.append_line("Podaj URL do pobrania.", "warning")
            return
        category = self._category.text().strip() or None
        organizer = self._organizer.text().strip() or None
        tags = [t.strip() for t in self._tags.text().split(",") if t.strip()]
        cookies_browser = self._browser.currentText() if self._use_cookies.isChecked() else None

        self._jobs.enqueue(
            JOB_DOWNLOAD,
            payload={
                "url": url,
                "library_root": str(cfg_mod.default_recordings_dir()),
                "audio_only": self._audio_only.isChecked(),
                "cookies_browser": cookies_browser,
                "title": self._title.text().strip() or None,
                "category": category,
                "organizer": organizer,
                "tags": tags,
                "cloud_ok": self._cloud_ok.isChecked(),
            },
        )
        self.enqueued_count += 1
        if self._save_profile.isVisible() and self._save_profile.isChecked():
            self._profiles.upsert(
                SourceProfile(
                    domain=domain_of(url),
                    category=category,
                    tags=tuple(tags),
                    organizer=organizer,
                    cloud_ok=self._cloud_ok.isChecked(),
                )
            )
        self._log.append_line(f"Dodano do kolejki pobierania: {url}", "ok")
        self.accept()
