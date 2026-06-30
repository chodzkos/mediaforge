"""Punkt wejścia GUI mediaforge — wpięcie chodzkos-gui-kit (S0).

Sekwencja: logowanie + globalny excepthook → ``QApplication`` → ``Config`` z kitu
(z debouncem przez ``QTimer`` po stronie GUI) → ``ThemeManager`` (motyw z configu)
→ ``MainWindow`` → ``attach_titlebar`` (DWM) → ``exec``.

Komponenty z kitu zamiast pisania od zera: PathEntry, FileList, LogView, HelpWindow
(+ helpery help_html), dialogi ``open_file``/``save_file``/``pick_dir``, ikony
``get_icon``/``ICON_MAP``. Własnego ``theme.py``/dialogów NIE piszemy.
"""

from __future__ import annotations

import sys

from chodzkos_gui_kit.config import Config
from chodzkos_gui_kit.qt.theme import ThemeManager
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from mediaforge.core import config
from mediaforge.core.logging_setup import install_excepthook, logger, setup_logging

# Opóźnienie debounce zapisu configu (kontrakt kitu: GUI trzyma timer).
_FLUSH_DELAY_MS = 1000


def _make_flush_timer(cfg: Config) -> QTimer:
    """Tworzy single-shot timer zapisujący config po ~1 s od ostatniej zmiany.

    ``Config.on_dirty`` (z kitu) restartuje timer przy każdej zmianie — zapis na
    dysk następuje dopiero, gdy zmiany ucichną (debounce). Timer żyje w GUI.
    """
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setInterval(_FLUSH_DELAY_MS)
    timer.timeout.connect(cfg.flush)
    cfg.on_dirty = timer.start
    return timer


def main() -> int:
    """Uruchamia aplikację GUI i zwraca kod wyjścia pętli zdarzeń."""
    log_path = setup_logging()
    install_excepthook()
    logger.info("Start mediaforge (log: %s)", log_path)

    app = QApplication(sys.argv)
    app.setApplicationName("mediaforge")

    cfg = config.load()
    flush_timer = _make_flush_timer(cfg)

    theme_manager = ThemeManager(app, cfg)
    theme_manager.apply(theme_manager.setting)  # motyw z configu (domyślnie auto)

    # Import okna po QApplication (widżety Qt wymagają istniejącej aplikacji).
    from mediaforge.gui.main_window import MainWindow

    window = MainWindow(theme_manager, cfg)
    theme_manager.attach_titlebar(window)
    window.show()
    window.start_jobs()  # wątek roboczy kolejki + polling statusów (po pokazaniu okna)

    exit_code = app.exec()
    flush_timer.stop()
    cfg.save_now()  # gwarancja zapisu przy zamknięciu
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
