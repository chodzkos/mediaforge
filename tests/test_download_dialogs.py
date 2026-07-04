"""Dialogi S5 (pytest-qt, offscreen): pobieranie, podcast, profile — ścieżki kolejkowania."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.core.engines.podcast import PodcastEpisode, PodcastFeed
from mediaforge.core.jobs import JobStore
from mediaforge.core.jobs.handlers import JOB_DOWNLOAD
from mediaforge.core.library.db import Database
from mediaforge.core.library.profiles import SourceProfile, SourceProfileStore
from mediaforge.gui.download_dialog import DownloadDialog
from mediaforge.gui.podcast_dialog import PodcastDialog
from mediaforge.gui.profiles_dialog import ProfilesDialog


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    monkeypatch.setattr(cfg_mod, "library_db_path", lambda: db)
    monkeypatch.setattr(cfg_mod, "default_recordings_dir", lambda: tmp_path / "lib")
    Database(db).migrate()
    return db


def test_download_dialog_enqueues_without_cookies(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    dialog = DownloadDialog()
    qtbot.addWidget(dialog)
    dialog._url.setText("https://vid.example.com/x")
    dialog._category.setText("Wykłady")
    dialog._on_download()

    jobs = JobStore(db).list_jobs()
    assert len(jobs) == 1 and jobs[0].job_type == JOB_DOWNLOAD
    payload = jobs[0].payload
    assert payload["url"] == "https://vid.example.com/x" and payload["category"] == "Wykłady"
    # Granica: bez opt-in cookies_browser jest None (żadnej sesji przeglądarki).
    assert payload["cookies_browser"] is None


def test_download_dialog_cookies_opt_in_flows_to_payload(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    dialog = DownloadDialog()
    qtbot.addWidget(dialog)
    dialog._url.setText("https://vid.example.com/x")
    dialog._use_cookies.setChecked(True)
    dialog._browser.setCurrentText("firefox")
    dialog._on_download()

    payload = JobStore(db).list_jobs()[0].payload
    assert payload["cookies_browser"] == "firefox"  # tylko po jawnym opt-in


def test_download_dialog_cookie_hint_visibility(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hint o cookies Chromium widoczny dopiero po opt-in i dla przeglądarki wymagającej hintu."""
    _isolate(monkeypatch, tmp_path)
    # Wymuś „przeglądarka wymaga hintu" niezależnie od platformy testowej (CI = Linux).
    monkeypatch.setattr(
        "mediaforge.gui.download_dialog.chromium_cookie_hint_needed", lambda _b: True
    )
    dialog = DownloadDialog()
    qtbot.addWidget(dialog)
    # Domyślnie cookies wyłączone → hint ukryty (nawet gdy przeglądarka byłaby „chromium").
    assert not dialog._cookie_hint.isVisibleTo(dialog)
    dialog._use_cookies.setChecked(True)  # opt-in → wołany _update_cookie_hint
    assert dialog._cookie_hint.isVisibleTo(dialog)


def test_podcast_dialog_loads_and_enqueues_selected(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    feed = PodcastFeed(
        title="Podcast X",
        episodes=(
            PodcastEpisode("Odc. 1", "https://cdn.example.com/1.mp3", "", "00:30:00", "opis"),
            PodcastEpisode("Odc. 2", "https://cdn.example.com/2.mp3", "", "", ""),
        ),
    )
    dialog = PodcastDialog(feed_loader=lambda _url: feed)
    qtbot.addWidget(dialog)
    dialog._url.setText("https://example.com/rss")
    dialog._on_load()
    assert dialog._list.count() == 2

    dialog._list.item(0).setCheckState(Qt.CheckState.Checked)  # tylko pierwszy odcinek
    dialog._on_download_selected()

    jobs = JobStore(db).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload["url"].endswith("1.mp3") and jobs[0].payload["title"] == "Odc. 1"


def test_podcast_dialog_prefills_from_profile(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    SourceProfileStore(db).upsert(
        SourceProfile(domain="example.com", category="Podcasty", cloud_ok=True)
    )
    feed = PodcastFeed(
        title="P", episodes=(PodcastEpisode("E", "https://cdn.example.com/e.mp3", "", "", ""),)
    )
    dialog = PodcastDialog(feed_loader=lambda _url: feed)
    qtbot.addWidget(dialog)
    dialog._url.setText("https://example.com/rss")
    dialog._on_load()
    dialog._list.item(0).setCheckState(Qt.CheckState.Checked)
    dialog._on_download_selected()

    payload = JobStore(db).list_jobs()[0].payload
    assert payload["category"] == "Podcasty" and payload["cloud_ok"] is True


def test_profiles_dialog_saves_profile(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    dialog = ProfilesDialog()
    qtbot.addWidget(dialog)
    dialog._domain.setText("Konf.Example.COM")
    dialog._category.setText("Konferencje")
    dialog._tags.setText("kardio, 2026")
    dialog._cloud_ok.setChecked(True)
    dialog._on_save()

    profile = SourceProfileStore(db).get("konf.example.com")  # domena znormalizowana
    assert profile is not None and profile.category == "Konferencje"
    assert profile.tags == ("kardio", "2026") and profile.cloud_ok is True
