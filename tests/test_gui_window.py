"""Start okna głównego (pytest-qt, offscreen) + About + persystencja geometrii."""

from __future__ import annotations

from pathlib import Path

import pytest
from chodzkos_gui_kit.config import Config
from chodzkos_gui_kit.qt.theme import ThemeManager
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.core import detection
from mediaforge.gui import main_window as mw
from mediaforge.gui.about import about_tabs

# Raport w kształcie detection.check_all() — jedno źródło detekcji (status bar == doctor).
_FAKE_REPORT = {
    "system": {"os": "Linux", "python": "3.12"},
    "ffmpeg": {"available": True, "version": "6.1", "encoders": {}},
    "whispercpp": {"available": False, "path": None},
    "ytdlp": {"available": False, "version": ""},
    "gpu": {"available": True, "name": "RTX 5090", "vram_gb": 24.0, "arch": "blackwell"},
    "compute": {"tier": "A", "note": "pełnia lokalnie"},
    "litellm": {"available": False, "base_url": ""},
    "providers": {},
}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(cfg_mod.APP_NAME, path=tmp_path / "config.json")


def _make_window(
    qtbot: QtBot, qapp: QApplication, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> mw.MainWindow:
    monkeypatch.setattr(detection, "check_all", lambda **_kwargs: _FAKE_REPORT)
    tm = ThemeManager(qapp, cfg)
    tm.apply(tm.setting)
    window = mw.MainWindow(tm, cfg)
    qtbot.addWidget(window)
    return window


def test_window_starts_with_status_and_log(
    qtbot: QtBot, qapp: QApplication, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _make_window(qtbot, qapp, cfg, monkeypatch)
    window.show()
    assert window.windowTitle() == "mediaforge"
    assert "Tier: A" in window._status_label.text()
    assert "FFmpeg: OK" in window._status_label.text()
    # Log startowy ma wpisy (gotowość + środowisko).
    assert "gotowy" in window._log.toPlainText()


def test_theme_cycle_updates_setting_and_config(
    qtbot: QtBot, qapp: QApplication, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _make_window(qtbot, qapp, cfg, monkeypatch)
    tm = window._theme_manager
    before = tm.setting
    window._cycle_theme()
    after = tm.setting
    assert after != before
    assert cfg.get(cfg_mod.THEME_KEY) == after  # zapisane do configu przez ThemeManager


def test_close_persists_geometry(
    qtbot: QtBot,
    qapp: QApplication,
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _make_window(qtbot, qapp, cfg, monkeypatch)
    window.show()
    window.close()
    # Świeży odczyt z dysku — geometria zapisana w closeEvent.
    reloaded = Config(cfg_mod.APP_NAME, path=tmp_path / "config.json")
    assert cfg_mod.get_window_geometry(reloaded) is not None


def test_about_tabs_carry_legal_notice() -> None:
    html = "".join(h for _, h in about_tabs())
    assert "legalny dostęp" in html
    assert "DRM" in html
    assert "#" not in html  # treść składana z palety, bez hexów
