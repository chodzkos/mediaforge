# Changelog

Wszystkie istotne zmiany w projekcie dokumentowane są w tym pliku.
Format oparty na [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/),
projekt stosuje [Semantic Versioning](https://semver.org/lang/pl/).

## [Unreleased]

### Added
- **S0 — Fundament.** Szkielet pakietu `core`/`gui`/`cli` z pełnym gate (ruff + mypy --strict + pytest), CI Windows.
  - **`core/config.py`** — cienka warstwa nad `Config` z chodzkos-gui-kit (platformdirs + atomowy zapis); typowane akcesory: motyw (odczyt), ostatnie katalogi, geometria okna, profil obliczeniowy per maszyna (nadpisanie tieru wg fingerprintu), rejestr dostawców per zadanie. Debounce realizuje GUI (`on_dirty` → `QTimer` → `flush()`).
  - **`core/secrets.py`** — keyring z ujednoliconym nazewnictwem kluczy per dostawca (`api_key:<provider>`) + token HF/Telegram.
  - **`core/logging_setup.py`** — logowanie do rotowanego pliku w katalogu logów (platformdirs) + globalny `sys.excepthook`.
  - **`core/tools.py`** — detekcja ffmpeg / whisper.cpp / CUDA (przez `nvidia-smi`, nazwa GPU → architektura) i złożenie profilu obliczeniowego (tier A/B/C).
  - **`core/library/`** — schemat SQLite (`recordings`, `jobs`, `transcripts`, `summaries`, `tags`, `settings`, `source_profiles`) + lekkie migracje przez `PRAGMA user_version`.
  - **`core/jobs/`** — kolejka: tabela `jobs` (status/progress/error/retry) + pula wątków (`ThreadPoolExecutor` ze std-lib — **nie QThread**, bo `core` nie importuje Qt; GUI jest adapterem). Bez Celery/Redis.
  - **`gui/`** — powłoka okna wpinająca chodzkos-gui-kit: `ThemeManager` (auto/jasny/ciemny + DWM titlebar), górny pasek §6 (logo + przełącznik motywu + „O programie"), `LogView` na status, dolny pasek z wykrytymi narzędziami (ffmpeg/whisper/CUDA + tier), persystencja geometrii przez `Config`. Zero hardcodowanych hexów; bez globalnego QSS. „O programie" przez kitowy `HelpWindow` z wymaganym komunikatem prawnym (`LEGAL_BOUNDARIES.md`).
  - **`cli/`** — szkielet Typer (`version`, `info`, `paths`).
  - Testy pytest-qt (offscreen): start okna, round-trip configu przez `Config` z kitu, kolejka zadań (retry), migracje SQLite, detekcja środowiska, strażnik „`core` bez Qt", CLI.

### Security
- Granice z `LEGAL_BOUNDARIES.md` respektowane: brak obchodzenia DRM/TPM i headless-bypassu logowania; sekrety wyłącznie w keyring (nie w configu/repo).
