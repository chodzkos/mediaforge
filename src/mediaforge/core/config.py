"""Konfiguracja mediaforge — cienka warstwa nad ``chodzkos_gui_kit.config.Config``.

Magazyn (platformdirs + zapis atomowy + flaga „dirty") pochodzi z gui-kit — nie
reimplementujemy go. Tutaj definiujemy nazwę aplikacji i **typowane akcesory** dla
kluczy preferencji mediaforge:

* motyw (klucz dzielony z ``ThemeManager`` kitu — tu tylko odczyt),
* ostatnie katalogi (per cel: import / biblioteka / eksport…),
* geometria okna (persystencja przez Config, nie QSettings),
* profil obliczeniowy per maszyna (nadpisanie tieru wg fingerprintu),
* rejestr dostawców per zadanie (``Task`` → ``ModelSpec``).

**Profile źródeł** (per domena) są danymi relacyjnymi — żyją w bibliotece SQLite
(``core.library``, tabela ``source_profiles``), nie tutaj, żeby nie dublować
magazynu i dało się je odpytywać po domenie (zgodnie z ARCHITECTURE.md).

Debounce realizuje GUI: ustawia ``on_dirty`` na callback restartujący ``QTimer``,
który po ~1 s woła ``config.flush()`` (kontrakt kitu). Rdzeń nie importuje Qt —
``chodzkos_gui_kit.config`` to czysty Python + platformdirs, więc import jest tu legalny.
"""

from __future__ import annotations

import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any

import platformdirs
from chodzkos_gui_kit.config import Config

from mediaforge.core.ai.providers import ModelSpec, Provider, Task
from mediaforge.core.compute import ComputeTier

APP_NAME = "mediaforge"

# Klucze configu (jedno źródło prawdy, bez literałów rozsianych po kodzie).
THEME_KEY = "theme"  # współdzielony z ThemeManager kitu (ten sam klucz)
_LAST_DIRS_KEY = "last_dirs"
_WINDOW_GEOMETRY_KEY = "window_geometry"
_COMPUTE_OVERRIDES_KEY = "compute_overrides"  # fingerprint maszyny → tier
_PROVIDER_ASSIGNMENTS_KEY = "provider_assignments"  # nazwa zadania → ModelSpec
_WHISPERCPP_PATH_KEY = "whispercpp_path"  # binarka whisper.cpp poza PATH (override sondy)
_LITELLM_BASE_URL_KEY = "litellm_base_url"  # endpoint gatewaya LiteLLM (override sondy)
_WHISPER_MODEL_KEY = "whisper_model"  # ścieżka do modelu whisper.cpp (.bin)
_WHISPER_LANGUAGE_KEY = "whisper_language"  # 'auto' | 'pl' | 'en' (domyślnie auto)
_WHISPER_THREADS_KEY = "whisper_threads"  # liczba wątków whisper-cli (opcjonalne)


def load(on_dirty: Callable[[], None] | None = None) -> Config:
    """Wczytaj konfigurację aplikacji (ścieżka z platformdirs / portable wg kitu)."""
    return Config(APP_NAME, on_dirty=on_dirty)


def machine_fingerprint() -> str:
    """Stabilny identyfikator maszyny (do profilu obliczeniowego per komputer)."""
    return f"{platform.node()}/{platform.machine()}".lower()


# ── Ścieżki aplikacji (platformdirs) ──────────────────────────────────────────


def data_dir() -> Path:
    """Katalog danych aplikacji (baza biblioteki, cache). Tworzony przy pierwszym użyciu."""
    path = Path(platformdirs.user_data_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def library_db_path() -> Path:
    """Ścieżka pliku bazy biblioteki (``library.sqlite3``)."""
    return data_dir() / "library.sqlite3"


def default_recordings_dir() -> Path:
    """Domyślny katalog na nagrania (``<wideo użytkownika>/mediaforge``)."""
    base = Path(platformdirs.user_videos_dir() or platformdirs.user_documents_dir())
    return base / APP_NAME


# ── Motyw (tylko odczyt — zapisuje ThemeManager kitu pod tym samym kluczem) ────


def get_theme(cfg: Config) -> str:
    """Zwraca zapisane ustawienie motywu (``auto``/``light``/``dark``)."""
    value = cfg.get(THEME_KEY)
    return value if value in ("auto", "light", "dark") else "auto"


# ── Ostatnie katalogi (per cel) ───────────────────────────────────────────────


def get_last_dir(cfg: Config, purpose: str) -> str | None:
    """Zwraca ostatnio użyty katalog dla danego celu (``import``/``library``…)."""
    dirs = cfg.get(_LAST_DIRS_KEY)
    if isinstance(dirs, dict):
        value = dirs.get(purpose)
        if isinstance(value, str):
            return value
    return None


def set_last_dir(cfg: Config, purpose: str, path: str) -> None:
    """Zapisuje ostatnio użyty katalog dla danego celu (oznacza config jako brudny)."""
    dirs = dict(cfg.get(_LAST_DIRS_KEY) or {})
    dirs[purpose] = path
    cfg[_LAST_DIRS_KEY] = dirs


# ── Geometria okna ────────────────────────────────────────────────────────────


def get_window_geometry(cfg: Config) -> str | None:
    """Zwraca zapisaną geometrię okna (base64 z ``QWidget.saveGeometry``) lub ``None``."""
    value = cfg.get(_WINDOW_GEOMETRY_KEY)
    return value if isinstance(value, str) else None


def set_window_geometry(cfg: Config, geometry_b64: str) -> None:
    """Zapisuje geometrię okna (string base64)."""
    cfg[_WINDOW_GEOMETRY_KEY] = geometry_b64


# ── Override'y sond detekcji (przekazywane do detection.check_all) ─────────────


def get_whispercpp_path(cfg: Config) -> str | None:
    """Ręcznie wskazana binarka whisper.cpp (poza PATH) lub ``None`` (autodetekcja)."""
    value = cfg.get(_WHISPERCPP_PATH_KEY)
    return value if isinstance(value, str) and value else None


def set_whispercpp_path(cfg: Config, path: str) -> None:
    """Zapisuje ścieżkę binarki whisper.cpp (override sondy)."""
    cfg[_WHISPERCPP_PATH_KEY] = path


def get_litellm_base_url(cfg: Config) -> str | None:
    """Endpoint gatewaya LiteLLM z configu lub ``None`` (sonda użyje domyślnego)."""
    value = cfg.get(_LITELLM_BASE_URL_KEY)
    return value if isinstance(value, str) and value else None


def set_litellm_base_url(cfg: Config, base_url: str) -> None:
    """Zapisuje endpoint gatewaya LiteLLM (override sondy)."""
    cfg[_LITELLM_BASE_URL_KEY] = base_url


def get_whisper_model(cfg: Config) -> str | None:
    """Ścieżka modelu whisper.cpp (.bin) z configu lub ``None`` (transkrypcja niedostępna)."""
    value = cfg.get(_WHISPER_MODEL_KEY)
    return value if isinstance(value, str) and value else None


def set_whisper_model(cfg: Config, path: str) -> None:
    """Zapisuje ścieżkę modelu whisper.cpp."""
    cfg[_WHISPER_MODEL_KEY] = path


def get_whisper_language(cfg: Config) -> str:
    """Język transkrypcji (``auto``/``pl``/``en``); domyślnie ``auto`` (autodetekcja)."""
    value = cfg.get(_WHISPER_LANGUAGE_KEY)
    return value if isinstance(value, str) and value else "auto"


def get_whisper_threads(cfg: Config) -> int | None:
    """Liczba wątków whisper-cli z configu lub ``None`` (domyślne whisper.cpp)."""
    value = cfg.get(_WHISPER_THREADS_KEY)
    return value if isinstance(value, int) and value > 0 else None


# ── Profil obliczeniowy per maszyna (nadpisanie tieru) ─────────────────────────


def get_compute_override(cfg: Config, fingerprint: str | None = None) -> ComputeTier | None:
    """Zwraca ręczne nadpisanie tieru dla maszyny (``None`` = używaj autodetekcji)."""
    fp = fingerprint if fingerprint is not None else machine_fingerprint()
    overrides = cfg.get(_COMPUTE_OVERRIDES_KEY)
    if isinstance(overrides, dict):
        raw = overrides.get(fp)
        if isinstance(raw, str):
            try:
                return ComputeTier(raw)
            except ValueError:
                return None
    return None


def set_compute_override(cfg: Config, tier: ComputeTier, fingerprint: str | None = None) -> None:
    """Zapisuje ręczne nadpisanie tieru dla maszyny."""
    fp = fingerprint if fingerprint is not None else machine_fingerprint()
    overrides = dict(cfg.get(_COMPUTE_OVERRIDES_KEY) or {})
    overrides[fp] = tier.value
    cfg[_COMPUTE_OVERRIDES_KEY] = overrides


# ── Rejestr dostawców per zadanie ─────────────────────────────────────────────


def _model_spec_to_dict(spec: ModelSpec) -> dict[str, Any]:
    return {
        "provider": spec.provider.value,
        "model": spec.model,
        "supports_vision": spec.supports_vision,
        "context_tokens": spec.context_tokens,
    }


def _model_spec_from_dict(data: dict[str, Any]) -> ModelSpec | None:
    try:
        provider = Provider(data["provider"])
        model = str(data["model"])
    except (KeyError, ValueError):
        return None
    return ModelSpec(
        provider=provider,
        model=model,
        supports_vision=bool(data.get("supports_vision", False)),
        context_tokens=int(data.get("context_tokens", 0)),
    )


def get_provider_assignments(cfg: Config) -> dict[Task, ModelSpec]:
    """Zwraca mapowanie zadanie → model (puste, gdy nic nie przypisano)."""
    raw = cfg.get(_PROVIDER_ASSIGNMENTS_KEY)
    result: dict[Task, ModelSpec] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                task = Task(key)
            except ValueError:
                continue
            if isinstance(value, dict):
                spec = _model_spec_from_dict(value)
                if spec is not None:
                    result[task] = spec
    return result


def set_provider_assignments(cfg: Config, assignments: dict[Task, ModelSpec]) -> None:
    """Zapisuje mapowanie zadanie → model do configu."""
    cfg[_PROVIDER_ASSIGNMENTS_KEY] = {
        task.value: _model_spec_to_dict(spec) for task, spec in assignments.items()
    }
