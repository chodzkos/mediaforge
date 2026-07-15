"""Pomoc (F1) + runbook + „O programie": zawartość i render z jednego pliku prawdy."""

from __future__ import annotations

from importlib.metadata import version

from PySide6.QtWidgets import QTabWidget
from pytestqt.qtbot import QtBot

from mediaforge.gui.about import about_tabs
from mediaforge.gui.help_window import build_help_window, infrastruktura_source

_EXPECTED_TABS = [
    "Szybki start",
    "Wymagania i instalacja",
    "Nagrywanie",
    "Transkrypcja",
    "Streszczenia",
    "Slajdy i notatki",
    "Pobieranie i podcasty",
    "Rozwiązywanie problemów",
]


def test_infrastruktura_md_exists_with_key_sections() -> None:
    """Runbook istnieje, jest niepusty i ma kluczowe sekcje (FFmpeg/Ollama/LiteLLM/diagnoza)."""
    path = infrastruktura_source()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.strip()
    for header in ("## FFmpeg", "## Ollama", "## LiteLLM", "## whisper.cpp", "## Szybka diagnoza"):
        assert header in text


def _tabs(window: object) -> QTabWidget:
    return window.findChild(QTabWidget)  # type: ignore[attr-defined,no-any-return]


def test_help_window_opens_with_all_sections(qtbot: QtBot) -> None:
    """Okno Pomocy buduje się bez błędu i ma wszystkie zakładki w zadanej kolejności."""
    window = build_help_window()
    qtbot.addWidget(window)
    tabs = _tabs(window)
    assert [tabs.tabText(i) for i in range(tabs.count())] == _EXPECTED_TABS


def test_install_section_renders_from_runbook_file(qtbot: QtBot) -> None:
    """Sekcja „Wymagania i instalacja" renderuje TREŚĆ z docs/INFRASTRUKTURA.md (jeden plik prawdy).

    Zakładka jest Markdown (nie HTML), a jej surowa treść to DOKŁADNIE tekst pliku — brak drugiej
    kopii runbooka w kodzie. Render przez ``setMarkdown`` przekłada nagłówki na tekst widoczny.
    """
    window = build_help_window()
    qtbot.addWidget(window)

    browser, content, markdown = window._browsers[1]  # druga zakładka = instalacja
    assert markdown is True
    assert content == infrastruktura_source().read_text(encoding="utf-8")
    rendered = browser.toPlainText()
    assert "FFmpeg" in rendered and "LiteLLM" in rendered  # treść runbooka wyrenderowana


def test_about_shows_version_from_importlib_metadata() -> None:
    """„O programie" pokazuje wersję z importlib.metadata (nie literał w kodzie)."""
    html = "".join(h for _, h in about_tabs())
    assert f"Wersja {version('mediaforge')}" in html
    # Stack wymieniony (PySide6/FFmpeg/whisper.cpp/Ollama/LiteLLM).
    assert "PySide6" in html and "whisper.cpp" in html and "LiteLLM" in html
