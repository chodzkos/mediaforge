"""Punkt wejścia GUI. Pełna powłoka okna powstaje w S0 wg GUI_STANDARD.md (gui-kit).

Kanoniczne wpięcie chodzkos-gui-kit (do zrealizowania w S0):

    from PySide6.QtWidgets import QApplication
    from chodzkos_gui_kit.qt.theme import ThemeManager
    from mediaforge.core import config

    app = QApplication([])
    cfg = config.load()                 # Config z kitu (platformdirs + atomowy zapis)
    tm = ThemeManager(app, cfg)         # Fusion + paleta + QSS; tryb z configu
    tm.apply("auto")                    # auto: styleHints().colorScheme(), Unknown → dark
    window = MainWindow()               # górny pasek: logo + przełącznik motywu + About (§6)
    tm.attach_titlebar(window)          # DWM titlebar (atrybut stanowy, bezwarunkowo)
    window.show()
    app.exec()

Komponenty z kitu zamiast pisania od zera: PathEntry (katalogi), FileList (import/biblioteka,
z D&D), LogView (streaming nagrywania/transkrypcji — parametr level_colors), HelpWindow
(okno pomocy z zakładkami + helpery help_html), dialogi open_file/open_files/save_file/pick_dir,
get_icon/ICON_MAP.
"""

from __future__ import annotations


def main() -> int:
    """Uruchom aplikację GUI (stub — implementacja w S0, patrz docs/PROMPTS.md)."""
    raise NotImplementedError("GUI shell — etap S0 (wpięcie chodzkos-gui-kit).")


if __name__ == "__main__":
    raise SystemExit(main())
