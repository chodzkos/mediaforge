"""Pomoc w aplikacji (F1) — kitowy ``HelpWindow`` z zakładkami.

Treść funkcjonalna to zwięzły HTML składany helperami ``help_html`` (kolory przez
``palette(...)``, zero hexów) — opisujemy to, co JEST w GUI, nie plany. Sekcja „Wymagania i
instalacja" NIE jest duplikowana w kodzie: renderowana jest wprost z ``docs/INFRASTRUKTURA.md``
przez ``HelpWindow.add_markdown_section`` (jeden plik prawdy — runbook infrastruktury).

Lokalizacja runbooka: w zainstalowanej aplikacji plik jest package-data
(``mediaforge/_resources/INFRASTRUKTURA.md`` — patrz ``force-include`` w pyproject), czytany
przez :mod:`importlib.resources`. W pracy z drzewa źródeł (uv run) fallback na repo ``docs/``.

Uwaga składniowa: łańcuchy treści delimitujemy APOSTROFEM (``'...'``), bo niosą polskie
cudzysłowy proste (``"``) — delimiter apostrofowy jest na nie odporny (polski tekst nie ma ``'``).
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from chodzkos_gui_kit.qt.widgets import HelpWindow, paragraph, section, unordered_list
from PySide6.QtWidgets import QWidget

HELP_TITLE = "Pomoc — mediaforge"

_INFRA_FILENAME = "INFRASTRUKTURA.md"
# Repo docs/ (fallback dev): src/mediaforge/gui/help_window.py -> parents[3] = root repo.
_REPO_DOCS = Path(__file__).resolve().parents[3] / "docs"


def infrastruktura_source() -> Path:
    """Ścieżka do ``INFRASTRUKTURA.md`` — package-data (app) albo repo ``docs/`` (dev).

    Zainstalowana aplikacja: plik jako package-data (``force-include`` w pyproject →
    ``mediaforge/_resources/``), lokalizowany przez :mod:`importlib.resources`. Praca z drzewa
    źródeł (uv run, brak package-data): fallback na repozytoryjne ``docs/`` — działa w obu trybach,
    bez duplikowania treści.
    """
    packaged = importlib.resources.files("mediaforge") / "_resources" / _INFRA_FILENAME
    if packaged.is_file():
        return Path(str(packaged))
    return _REPO_DOCS / _INFRA_FILENAME


def _quick_start() -> str:
    return section(
        "Szybki start",
        paragraph("Typowy przepływ pracy z materiałem:")
        + unordered_list(
            '<b>Zdobądź materiał</b>: „● Nagrywaj" (ekran/dźwięk), „Importuj" (plik z dysku) '
            'albo „Pobierz…"/„Podcast…" (yt-dlp / RSS).',
            '<b>Transkrybuj</b> — przycisk „Transkrybuj" (whisper.cpp na GPU); postęp w %.',
            '<b>Streść</b> — przycisk „Streść" (gateway LiteLLM); aktywny po transkrypcji.',
            '<b>Notatka</b> — przycisk „Notatka" (analiza slajdów VLM + komentarz); '
            "aktywny, gdy materiał ma slajdy i transkrypt.",
        )
        + paragraph(
            "Nagrywanie i transkrypcja działają lokalnie, bez gatewaya. Streszczenia i notatki "
            'wymagają uruchomionego gatewaya LiteLLM (patrz „Wymagania i instalacja").'
        ),
    )


def _recording() -> str:
    return section(
        "Nagrywanie",
        unordered_list(
            "<b>Źródło audio systemowego</b>: wymaga VB-Cable (wirtualny kabel) — ustawienia w "
            'zakładce „Wymagania i instalacja".',
            '<b>Pre-roll</b>: po „Start" jest „Przygotowuję…", potem „● Nagrywam" — zimny start '
            'kompozytora przypada przed treścią (licznik rusza od „Nagrywam").',
            "<b>Region / monitor</b>: nagrywasz cały monitor albo wskazany region; wybór monitora "
            "przy wielu ekranach.",
            "<b>Enkoder</b>: sprzętowy (NVENC/AMF/QSV) gdy dostępny, inaczej software (CPU) z "
            "ograniczeniem fps/rozdzielczości — wybrany enkoder widać w logu.",
        ),
    )


def _transcription() -> str:
    return section(
        "Transkrypcja",
        paragraph(
            '„Transkrybuj" dodaje zadanie whisper.cpp (CUDA) do kolejki; postęp w procentach w '
            "logu. Wynik to napisy i tekst w folderze materiału."
        )
        + paragraph(
            "Wymaga skonfigurowanej binarki i modelu whisper.cpp (klucze "
            "<code>whispercpp_path</code>/<code>whisper_model</code>) — sprawdź „Wymagania i "
            'instalacja" oraz <code>doctor</code>.'
        ),
    )


def _summaries() -> str:
    return section(
        "Streszczenia",
        paragraph(
            '„Streść" streszcza transkrypt przez gateway LiteLLM → <code>summary.md</code> w '
            "folderze materiału (podgląd w panelu; badge 🧾 na liście po ukończeniu)."
        )
        + paragraph(
            '<b>Prywatność (fail-safe)</b>: bez zaznaczenia „Zezwól na przetwarzanie w chmurze" '
            "(<code>cloud_ok</code>) materiał idzie <b>wyłącznie do modelu lokalnego</b>. Zgoda "
            "jest per materiał i domyślnie wyłączona."
        ),
    )


def _slides_notes() -> str:
    return section(
        "Slajdy i notatki",
        unordered_list(
            "<b>Podłącz slajdy</b>: skopiuj obrazy slajdów (z własnej przeglądarki) do materiału; "
            "nazwy z czasem (mp.pl <code>..._450s.png</code>) mapują slajd na moment nagrania.",
            "<b>Notatka</b>: dla materiału ze slajdami + transkryptem powstaje "
            "<code>notes.md</code> — sekcja per slajd: obraz, zakres czasu, komentarz "
            "prowadzącego (ze streszczenia fragmentu transkryptu) i najważniejsze punkty.",
        )
        + paragraph(
            "Analiza slajdu (VLM) i komentarz (LLM) idą <b>fazami</b> (najpierw wszystkie slajdy, "
            "potem komentarze) — jeden model w VRAM naraz. Ta sama granica prywatności co "
            "streszczenia (<code>cloud_ok</code>)."
        ),
    )


def _downloading() -> str:
    return section(
        "Pobieranie i podcasty",
        unordered_list(
            '<b>Pobierz…</b>: URL do wideo/audio (yt-dlp); opcja „tylko audio".',
            "<b>Treści za logowaniem</b>: sesja przeglądarki tylko przez <b>Firefox</b> (zaloguj "
            "się i zamknij Firefoksa przed pobraniem) — cookies Chrome/Edge na Windows są "
            "nieodczytywalne.",
            "<b>Podcast…</b>: adres RSS → lista odcinków → wybrane trafiają do kolejki.",
            "<b>Profile źródeł</b>: per domena prefillują kategorię/tagi/organizatora i zgodę na "
            "chmurę.",
        ),
    )


def _troubleshooting() -> str:
    return section(
        "Rozwiązywanie problemów",
        unordered_list(
            "<b>Diagnostyka</b>: <code>uv run mediaforge-cli doctor</code> pokazuje stan "
            "wszystkiego; przy ✗ jest podpowiedź przyczyny.",
            "<b>Streszczenie/notatka wisi lub pada</b>: sprawdź, czy gateway LiteLLM chodzi "
            "(osobny terminal; <code>/v1/models</code>).",
            "<b>Model wolny</b>: <code>ollama ps</code> w trakcie — PROCESSOR ma być 100% GPU.",
            "<b>Nagranie szarpie</b>: log pokazuje enkoder (GPU vs CPU); statystyki dup/drop w "
            "folderze materiału.",
        )
        + paragraph('Pełny runbook infrastruktury — zakładka „Wymagania i instalacja".'),
    )


def build_help_window(parent: QWidget | None = None) -> HelpWindow:
    """Buduje okno Pomocy: zakładki funkcjonalne (HTML) + instalacja (Markdown z pliku).

    Sekcje dokładane w kolejności — „Wymagania i instalacja" jako druga, renderowana wprost z
    ``docs/INFRASTRUKTURA.md`` (jeden plik prawdy). Zwraca okno bez ``exec`` (testowalne).
    """
    window = HelpWindow(parent, title=HELP_TITLE)
    window.add_html_section("Szybki start", _quick_start())
    window.add_markdown_section("Wymagania i instalacja", infrastruktura_source())
    window.add_html_section("Nagrywanie", _recording())
    window.add_html_section("Transkrypcja", _transcription())
    window.add_html_section("Streszczenia", _summaries())
    window.add_html_section("Slajdy i notatki", _slides_notes())
    window.add_html_section("Pobieranie i podcasty", _downloading())
    window.add_html_section("Rozwiązywanie problemów", _troubleshooting())
    return window


def open_help(parent: QWidget | None = None) -> None:
    """Otwiera modalne okno Pomocy (F1)."""
    build_help_window(parent).exec()
