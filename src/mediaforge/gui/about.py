"""Okno „O programie" — kitowy ``HelpWindow`` z zakładkami (treść + granice prawne).

Niesie wymagany komunikat z ``LEGAL_BOUNDARIES.md`` (legalny dostęp do materiałów).
Treść składana helperami ``help_html`` (kolory przez ``palette(...)``, zero hexów).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from chodzkos_gui_kit.qt.widgets import HelpWindow, paragraph, section
from PySide6.QtWidgets import QWidget

ABOUT_TITLE = "O programie — mediaforge"


def _version() -> str:
    """Wersja z metadanych zainstalowanego pakietu (jedno źródło prawdy — pyproject).

    Praca z drzewa źródeł bez instalacji → ``0.0.0+unknown`` (jak w gui-kit); nie sięgamy po
    literał w kodzie (koniec rozjazdu ``__version__`` vs pyproject).
    """
    try:
        return _pkg_version("mediaforge")
    except PackageNotFoundError:
        return "0.0.0+unknown"


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
        paragraph(f"Wersja {_version()}")
        + paragraph(
            "Desktopowy archiwizator materiałów edukacyjnych z transkrypcją i "
            "streszczeniami AI (Windows)."
        )
        + paragraph(
            "Stack: PySide6 (GUI) · FFmpeg (nagrywanie/konwersja) · whisper.cpp "
            "(transkrypcja) · Ollama + LiteLLM (streszczenia i analiza slajdów lokalnie)."
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
    slides = section(
        "Jak dodać slajdy",
        paragraph(
            "Slajdy zapisujesz z <b>własnej przeglądarki</b> (Twój ekran, Twoja sesja): "
            "rozszerzenie typu <i>Image Downloader</i> ściąga wszystkie obrazy z karty jednym "
            "kliknięciem, albo ręcznie prawy przycisk → „Zapisz obraz”. Potem w bibliotece, "
            "przy materiale, użyj <b>„Podłącz slajdy”</b> i wskaż zapisane pliki (lub folder)."
        )
        + paragraph(
            "Jeśli nazwy plików niosą czas (np. z mp.pl: <code>..._450s.png</code>), mediaforge "
            "automatycznie zmapuje slajdy do momentów nagrania — miniatura pokaże znacznik czasu."
        ),
    )
    return [("O programie", about), ("Granice prawne", legal), ("Slajdy", slides)]


def open_about(parent: QWidget | None = None) -> None:
    """Otwiera modalne okno „O programie" (kitowy HelpWindow)."""
    HelpWindow(parent, title=ABOUT_TITLE, tabs=about_tabs()).exec()
