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


def test_help_menu_has_help_f1_and_about(
    qtbot: QtBot, qapp: QApplication, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Menu „Pomoc" ma akcję Pomoc (skrót F1) i „O programie"."""
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import QMenu

    window = _make_window(qtbot, qapp, cfg, monkeypatch)
    menus = {m.title(): m for m in window.menuBar().findChildren(QMenu)}
    assert "&Pomoc" in menus
    actions = {a.text(): a for a in menus["&Pomoc"].actions() if a.text()}
    assert "&Pomoc" in actions and "&O programie" in actions
    # Pomoc pod F1 (StandardKey.HelpContents mapuje się na F1).
    assert actions["&Pomoc"].shortcut() == QKeySequence(QKeySequence.StandardKey.HelpContents)


def test_window_starts_with_status_and_log(
    qtbot: QtBot, qapp: QApplication, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _make_window(qtbot, qapp, cfg, monkeypatch)
    window.show()
    assert window.windowTitle() == "mediaforge"
    # Sonda środowiska idzie w tle — status startuje placeholderem i wypełnia się po sygnale.
    assert "Wykrywanie środowiska" in window._status_label.text()
    qtbot.waitUntil(lambda: "Tier: A" in window._status_label.text(), timeout=5000)
    assert "FFmpeg: OK" in window._status_label.text()
    # Log startowy ma wpisy (gotowość + środowisko).
    assert "gotowy" in window._log.toPlainText()


def test_environment_probe_off_ui_thread_no_subprocess(
    qtbot: QtBot, qapp: QApplication, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sonda środowiska idzie w tle (zamockowany check_all) — start okna nie woła subprocess."""
    import subprocess

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("subprocess nie powinien być wołany przy starcie okna głównego")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    calls: list[dict[str, object]] = []

    def fake_check_all(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)  # gdyby to była realna sonda, wołałaby subprocess (tu zablokowany)
        return _FAKE_REPORT

    monkeypatch.setattr(detection, "check_all", fake_check_all)

    tm = ThemeManager(qapp, cfg)
    tm.apply(tm.setting)
    window = mw.MainWindow(tm, cfg)
    qtbot.addWidget(window)

    # Zaraz po konstrukcji: placeholder — sonda w tle jeszcze nie wróciła (nie zablokowała UI).
    assert "Wykrywanie środowiska" in window._status_label.text()
    # Sygnał wypełnia status po sondzie.
    qtbot.waitUntil(lambda: "Tier: A" in window._status_label.text(), timeout=5000)
    assert calls  # check_all faktycznie wywołany (na wątku puli)
    assert window._env_report is not None  # raport zacache'owany (m.in. ffmpeg dla RecordDialog)


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
