"""Dialog podcastu — URL kanału RSS → lista odcinków (checkboxy) → joby pobrania audio.

Odcinki pobierane po bezpośrednim URL enclosure (yt-dlp łyka bezpośrednie mp3). Metadane
odcinka (tytuł, opis, data) trafiają do ``metadata.json`` materiału. Domyślne kategoria/tagi/
organizator + ``cloud_ok`` z profilu domeny feedu (fail-safe: brak profilu = lokalnie).
BEZ auto-subskrypcji (scheduler = backlog).
"""

from __future__ import annotations

from collections.abc import Callable

from chodzkos_gui_kit.qt.widgets import LogView
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.engines.download_engine import domain_of
from mediaforge.core.engines.podcast import PodcastEpisode, PodcastFeed, fetch_podcast_feed
from mediaforge.core.jobs import JobStore
from mediaforge.core.jobs.handlers import JOB_DOWNLOAD
from mediaforge.core.library.db import Database
from mediaforge.core.library.profiles import SourceProfileStore

FeedLoader = Callable[[str], PodcastFeed]


class PodcastDialog(QDialog):
    """Kanał RSS → wybór odcinków → joby pobrania (kind ``download``, audio z enclosure)."""

    def __init__(
        self, parent: QWidget | None = None, *, feed_loader: FeedLoader | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Podcast (RSS)")
        self.setMinimumWidth(600)
        self.enqueued_count = 0
        self._loader: FeedLoader = feed_loader or (lambda url: fetch_podcast_feed(url))
        self._episodes: list[PodcastEpisode] = []
        self._feed_url = ""

        db_path = cfg_mod.library_db_path()
        Database(db_path).migrate()
        self._jobs = JobStore(db_path)
        self._profiles = SourceProfileStore(db_path)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("URL kanału RSS:"))
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://…/feed.xml")
        self._url.returnPressed.connect(self._on_load)
        bar.addWidget(self._url, stretch=1)
        self._load_btn = QPushButton("Wczytaj")
        self._load_btn.clicked.connect(self._on_load)
        bar.addWidget(self._load_btn)
        root.addLayout(bar)

        self._title_label = QLabel("")
        self._title_label.setEnabled(False)
        root.addWidget(self._title_label)

        self._list = QListWidget()
        root.addWidget(self._list, stretch=1)

        self._log = LogView(timestamps=True)
        self._log.setMinimumHeight(90)
        root.addWidget(self._log)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Pobierz zaznaczone")
        ok.clicked.connect(self._on_download_selected)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        root.addWidget(buttons)

    def _on_load(self) -> None:
        url = self._url.text().strip()
        if not url:
            self._log.append_line("Podaj URL kanału RSS.", "warning")
            return
        try:
            feed = self._loader(url)
        except ValueError as exc:  # nie-RSS / błąd sieci — czytelny komunikat
            self._log.append_line(str(exc), "error")
            return
        self._feed_url = url
        self._episodes = list(feed.episodes)
        self._title_label.setText(f"{feed.title} — odcinków z audio: {len(self._episodes)}")
        self._list.clear()
        for ep in self._episodes:
            meta = " · ".join(p for p in (ep.published[:16], ep.duration) if p)
            item = QListWidgetItem(f"{ep.title}  ({meta})" if meta else ep.title)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._list.addItem(item)

    def _on_download_selected(self) -> None:
        profile = self._profiles.get(domain_of(self._feed_url)) if self._feed_url else None
        selected = [
            self._episodes[i]
            for i in range(self._list.count())
            if self._list.item(i).checkState() is Qt.CheckState.Checked
        ]
        if not selected:
            self._log.append_line("Zaznacz odcinki do pobrania.", "warning")
            return
        for ep in selected:
            self._jobs.enqueue(
                JOB_DOWNLOAD,
                payload={
                    "url": ep.audio_url,
                    "library_root": str(cfg_mod.default_recordings_dir()),
                    "title": ep.title,
                    "description": ep.description or None,
                    "category": profile.category if profile else None,
                    "organizer": profile.organizer if profile else None,
                    "tags": list(profile.tags) if profile else [],
                    "cloud_ok": profile.cloud_ok if profile else False,
                },
            )
            self.enqueued_count += 1
            self._log.append_line(f"W kolejce: {ep.title}", "ok")
        if self.enqueued_count:
            self.accept()
