"""Okno „O programie" — kitowy ``HelpWindow`` z zakładkami (treść + granice prawne).

Niesie wymagany komunikat z ``LEGAL_BOUNDARIES.md`` (legalny dostęp do materiałów).
Treść składana helperami ``help_html`` (kolory przez ``palette(...)``, zero hexów).
"""

from __future__ import annotations

from chodzkos_gui_kit.qt.widgets import HelpWindow, paragraph, section
from PySide6.QtWidgets import QWidget

from mediaforge import __version__

ABOUT_TITLE = "O programie — mediaforge"

# Komunikat wymagany przez LEGAL_BOUNDARIES.md (UI/README).
_LEGAL_NOTICE = (
    "Aplikacja jest przeznaczona do archiwizacji materiałów, do których masz "
    "<b>legalny dostęp</b>. Przed pobraniem/nagraniem sprawdź regulamin źródła "
    "i licencję materiału."
)


def about_tabs() -> list[tuple[str, str]]:
    """Zakładki okna „O programie" jako ``(tytuł, html)``."""
    about = section(
        "mediaforge",
        paragraph(f"Wersja {__version__}")
        + paragraph(
            "Desktopowy archiwizator materiałów edukacyjnych z transkrypcją i "
            "streszczeniami AI (Windows)."
        )
        + paragraph("Licencja: MIT")
        + paragraph(
            'Kod: <a href="https://github.com/chodzkos/mediaforge">github.com/chodzkos/mediaforge</a>'
        ),
    )
    legal = section(
        "Granice prawne",
        paragraph(_LEGAL_NOTICE)
        + paragraph(
            "Aplikacja <b>nie</b> obchodzi DRM ani zabezpieczeń dostępu i nie "
            "automatyzuje omijania logowania. Logowanie odbywa się przez sesję "
            "utworzoną samodzielnie przez użytkownika."
        ),
    )
    return [("O programie", about), ("Granice prawne", legal)]


def open_about(parent: QWidget | None = None) -> None:
    """Otwiera modalne okno „O programie" (kitowy HelpWindow)."""
    HelpWindow(parent, title=ABOUT_TITLE, tabs=about_tabs()).exec()
