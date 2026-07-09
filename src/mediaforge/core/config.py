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
_RECORD_PREROLL_KEY = "record_preroll_sec"  # sekundy głowy nagrania odcięte (zimny start ddagrab)
_SUMMARY_MODEL_LOCAL_KEY = "summary_model_local"  # model lokalny gatewaya (np. ollama/qwen3:27b)
_SUMMARY_MODEL_CLOUD_KEY = "summary_model_cloud"  # model chmurowy (None = brak trasy chmurowej)
_SUMMARY_LANGUAGE_KEY = "summary_language"  # język streszczenia (domyślnie pl)
_SUMMARY_MAX_TOKENS_KEY = "summary_max_tokens"  # limit tokenów odpowiedzi streszczenia
_SUMMARY_TIMEOUT_KEY = "summary_timeout_sec"  # timeout żądania do gatewaya (domyślnie 600 s)
_SUMMARY_PROMPT_SUFFIX_KEY = "summary_prompt_suffix"  # sufiks system-promptu (qwen3: /no_think)
_SUMMARY_CHUNK_CHARS_KEY = "summary_chunk_chars"  # próg podziału transkryptu (map-reduce)

# ~8k tokenów promptu — komfortowo w oknie 32k z miejscem na wyjście; powyżej tego progu
# streszczenie idzie ścieżką map-reduce (kawałki po granicach segmentów), poniżej: jeden request.
DEFAULT_SUMMARY_CHUNK_CHARS = 24_000


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


def get_summary_model_local(cfg: Config) -> str | None:
    """Model lokalny streszczeń (przez gateway, np. ``ollama/qwen3:27b``) lub ``None``."""
    value = cfg.get(_SUMMARY_MODEL_LOCAL_KEY)
    return value if isinstance(value, str) and value else None


def set_summary_model_local(cfg: Config, model: str) -> None:
    """Zapisuje nazwę modelu lokalnego streszczeń."""
    cfg[_SUMMARY_MODEL_LOCAL_KEY] = model


def get_summary_model_cloud(cfg: Config) -> str | None:
    """Model chmurowy streszczeń (przez gateway) lub ``None`` — ``None`` = brak trasy chmurowej."""
    value = cfg.get(_SUMMARY_MODEL_CLOUD_KEY)
    return value if isinstance(value, str) and value else None


def set_summary_model_cloud(cfg: Config, model: str) -> None:
    """Zapisuje nazwę modelu chmurowego streszczeń."""
    cfg[_SUMMARY_MODEL_CLOUD_KEY] = model


def get_summary_language(cfg: Config) -> str:
    """Język streszczenia (domyślnie ``pl``)."""
    value = cfg.get(_SUMMARY_LANGUAGE_KEY)
    return value if isinstance(value, str) and value else "pl"


def get_summary_max_tokens(cfg: Config) -> int:
    """Limit tokenów odpowiedzi streszczenia (domyślnie 4096).

    4096, bo modele rozumujące (qwen3) część budżetu zjadają na wewnętrzne rozumowanie zanim
    zaczną treść — za mały limit kończył się pustym streszczeniem.
    """
    value = cfg.get(_SUMMARY_MAX_TOKENS_KEY)
    return value if isinstance(value, int) and value > 0 else 4096


def get_summary_prompt_suffix(cfg: Config) -> str:
    """Sufiks system-promptu streszczenia (domyślnie ``/no_think`` — soft-switch qwen3).

    Konfigurowalny, by przy zmianie modelu dało się wyczyścić (``""``) bez zmiany kodu.
    Pusty łańcuch jest respektowany (jawne wyłączenie); brak klucza = domyślny ``/no_think``.
    """
    value = cfg.get(_SUMMARY_PROMPT_SUFFIX_KEY)
    return value if isinstance(value, str) else "/no_think"


def get_summary_timeout(cfg: Config) -> float:
    """Timeout żądania do gatewaya w sekundach (domyślnie 600 — długi materiał lokalnie).

    600 s, bo 2-godzinny transkrypt to kilkuminutowy prefill+generacja na lokalnym modelu 27b —
    dawne 120 s padało w połowie pracy (mylnie jako „gateway niedostępny", patrz SummaryClient).
    """
    value = cfg.get(_SUMMARY_TIMEOUT_KEY)
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return 600.0


def get_summary_chunk_chars(cfg: Config) -> int:
    """Próg podziału transkryptu na kawałki map-reduce (domyślnie 24000 znaków).

    Transkrypt dłuższy niż ten próg jest cięty na kawałki po granicach segmentów whispera
    (map -> reduce); krótszy idzie jedną, dotychczasową ścieżką (jeden request). 24000 znaków
    to ~8k tokenów promptu — mieści się w oknie 32k z zapasem na wyjście i wewnętrzne
    rozumowanie modelu. Wartość <= 0 (błędna) spada na default.
    """
    value = cfg.get(_SUMMARY_CHUNK_CHARS_KEY)
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_SUMMARY_CHUNK_CHARS


def get_record_preroll_sec(cfg: Config) -> int:
    """Pre-roll nagrania (UX): ile sekund GUI pokazuje „Przygotowuję…" przed „Nagrywam".

    Domyślnie 5 (zmierzony transient zimnego startu ddagrab to ~6 s). To WYŁĄCZNIE odczekanie
    w GUI — użytkownik zaczyna treść dopiero po sygnale, więc szarpana głowa przypada na czas
    przed treścią. FFmpeg NIE tnie (trim samego wideo rozjeżdżał A/V).
    """
    value = cfg.get(_RECORD_PREROLL_KEY)
    return value if isinstance(value, int) and value >= 0 else 5


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
