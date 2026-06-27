"""Konfiguracja mediaforge — cienka warstwa nad ``chodzkos_gui_kit.config.Config``.

Magazyn (platformdirs + zapis atomowy + flaga „dirty") pochodzi z gui-kit — nie
reimplementujemy go. Tutaj definiujemy tylko nazwę aplikacji i (docelowo) typowane
akcesory dla kluczy mediaforge: profil obliczeniowy per maszyna, rejestr dostawców,
profile źródeł, ostatnie katalogi, motyw.

Debounce realizuje GUI: ustawia ``on_dirty`` na callback restartujący ``QTimer``,
który po ~1 s woła ``config.flush()`` (kontrakt kitu). Rdzeń nie importuje Qt —
``chodzkos_gui_kit.config`` to czysty Python + platformdirs, więc import jest tu legalny.
"""

from __future__ import annotations

from collections.abc import Callable

from chodzkos_gui_kit.config import Config

APP_NAME = "mediaforge"


def load(on_dirty: Callable[[], None] | None = None) -> Config:
    """Wczytaj konfigurację aplikacji (ścieżka z platformdirs / portable wg kitu)."""
    return Config(APP_NAME, on_dirty=on_dirty)
