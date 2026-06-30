# Changelog

Wszystkie istotne zmiany w projekcie dokumentowane są w tym pliku.
Format oparty na [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/),
projekt stosuje [Semantic Versioning](https://semver.org/lang/pl/).

## [Unreleased]

### Changed
- Usunięto nieużywaną tabelę `settings` ze schematu SQLite (martwy kod z S0) — preferencje skalarne trzyma `config.json` (`core/config.py`), zgodnie z poprawionym ARCHITECTURE/PROMPTS. Schemat v1 edytowany w miejscu (nic nie shipnęło → bez migracji v1→v2).

### Added
- **S2 — Import lokalny + biblioteka.** Układ „jeden materiał = jeden folder" z `metadata.json` jako **źródłem prawdy** synchronizowanym z SQLite (round-trip folder ↔ baza).
  - **`core/library/material.py`** — model `MaterialMetadata` (tytuł, data, źródło, prowadzący, organizator, kategoria, tagi, długość, ścieżki, statusy transkrypcji/streszczenia) + IO `metadata.json` (ścieżki względne do folderu = przenośność, tagi kanoniczne).
  - **`core/engines/import_engine.py`** — `ImporterEngine` (Protocol `AcquisitionEngine`): import MP4/MKV/MOV/WEBM/AVI + MP3/WAV/M4A/AAC/FLAC/OGG; czyste, testowalne buildery komend FFmpeg (ekstrakcja audio, miniatura, ffprobe długości) + wstrzykiwane runnery; folder materiału + `metadata.json` + wpis SQLite.
  - **`core/library/recordings.py`** — `RecordingStore.upsert_material`/`to_metadata`/`list_materials` (filtry po tagu/kategorii) + `all_tags`/`all_categories` + **`rescan`** (odbudowa indeksu SQLite z `metadata.json` w folderach — czyni „folder = źródło prawdy" realnym: przeżywa skasowanie bazy, przeniesienie biblioteki, ręczną edycję; prune z guardem — NIE kasuje indeksu, gdy root niedostępny/pusto rozwiązany, bezpieczne dla biblioteki na NAS-ie/sieci); rekorder ujednolicony do tego samego układu (`metadata.json` + upsert).
  - **`gui/import_dialog.py`** — import na kitowym `FileList` (drag&drop) + `PathEntry`, wspólne metadane, status w `LogView`.
  - **`gui/library_widget.py`** — biblioteka: lista materiałów, filtr po kategorii/tagu, edycja metadanych (zapis do `metadata.json` + SQLite), podgląd miniatury, „Otwórz folder", „Przeskanuj" (rescan); wpięta w okno główne.
  - **Świadomy dług (spłata w S3):** import jest **synchroniczny** (kopia+FFmpeg blokują UI). Docelowo przez kolejkę `jobs` (ThreadPoolExecutor + `QTimer`) — przepięcie importu i transkrypcji w S3; tymczasowy QThread byłby duplikacją wyrzucaną przy S3. Profile źródeł (per domena) **odroczone do S5** (pasują do URL-i/downloadu; tabela `source_profiles` została w schemacie).
- **Enumeracja urządzeń audio dshow + detekcja loopbacku.** `core/engines/dshow_devices.py` (parser czysty + wrapper `ffmpeg -list_devices`, Windows-only); GUI nagrywania zamienia ręczne pola na edytowalne `QComboBox` (loopback / mikrofon) z `alt_name` jako jednoznacznym `-i audio=`, a przy braku urządzenia loopback pokazuje ostrzeżenie (ffmpeg nie ma natywnego WASAPI — dźwięk systemowy wymaga Stereo Mix / VB-Cable, czego enumeracja nie tworzy).
- **S1 — Nagrywanie ekranu i audio (rdzeń produktu).** `RecorderEngine` (Protocol `AcquisitionEngine`) + `RecorderSession`.
  - **`core/engines/ffmpeg_cmd.py`** — czysta (testowalna) budowa komend FFmpeg: źródła cały pulpit / monitor (region DPI-aware) / okno / region; wybór enkodera **NVENC (HEVC/AV1) z fallbackiem programowym** (`select_video_encoder` schodzi łańcuchem do dostępnego enkodera z buildu); audio przez `dshow` — dźwięk systemowy (WASAPI loopback) i/lub mikrofon, miks (`amix`); tryb tylko-audio; **presety** Ekonomiczny/Standard/Wysoka/Archiwum/Tylko audio; estymacja rozmiaru.
  - **`core/engines/segments.py`** — **crash-safe** segmentacja (`-f segment`) + odzysk: sklejenie ważnych segmentów demuxerem `concat` (kopia strumieni); puste/przerwane segmenty pomijane — częściowe nagranie pozostaje odzyskiwalne.
  - **`core/engines/recorder.py`** — maszyna stanów start → pauza ⇄ wznowienie → stop (ciągła numeracja segmentów), FFmpeg jako **subprocess** (wstrzykiwalny, więc testowalny bez FFmpeg), licznik czasu i szacowany rozmiar; po zakończeniu folder materiału + wpis w bibliotece (status `recorded`).
  - **`core/library/recordings.py`** — `RecordingStore` (CRUD tabeli `recordings`, statusy).
  - **`gui/record_dialog.py`** — dialog nagrywania na widgetach kitu (`PathEntry` na katalog, `LogView` ze statusami przez `level_colors`, np. `recording`); wybór monitora DPI-aware (`QScreen × devicePixelRatio`), presety/audio, timer i szacowany rozmiar przez `QTimer`, Start/Pauza/Stop; wpięty przyciskiem „● Nagrywaj" w górnym pasku.
- **S0 — Fundament.** Szkielet pakietu `core`/`gui`/`cli` z pełnym gate (ruff + mypy --strict + pytest), CI Windows.
  - **`core/config.py`** — cienka warstwa nad `Config` z chodzkos-gui-kit (platformdirs + atomowy zapis); typowane akcesory: motyw (odczyt), ostatnie katalogi, geometria okna, profil obliczeniowy per maszyna (nadpisanie tieru wg fingerprintu), rejestr dostawców per zadanie. Debounce realizuje GUI (`on_dirty` → `QTimer` → `flush()`).
  - **`core/secrets.py`** — keyring z ujednoliconym nazewnictwem kluczy per dostawca (`api_key:<provider>`) + token HF/Telegram.
  - **`core/logging_setup.py`** — logowanie do rotowanego pliku w katalogu logów (platformdirs) + globalny `sys.excepthook`.
  - **`core/tools.py`** — detekcja ffmpeg / whisper.cpp / CUDA (przez `nvidia-smi`, nazwa GPU → architektura) i złożenie profilu obliczeniowego (tier A/B/C).
  - **`core/library/`** — schemat SQLite (`recordings`, `jobs`, `transcripts`, `summaries`, `tags`, `source_profiles`) + lekkie migracje przez `PRAGMA user_version`.
  - **`core/jobs/`** — kolejka: tabela `jobs` (status/progress/error/retry) + pula wątków (`ThreadPoolExecutor` ze std-lib — **nie QThread**, bo `core` nie importuje Qt; GUI jest adapterem). Bez Celery/Redis.
  - **`gui/`** — powłoka okna wpinająca chodzkos-gui-kit: `ThemeManager` (auto/jasny/ciemny + DWM titlebar), górny pasek §6 (logo + przełącznik motywu + „O programie"), `LogView` na status, dolny pasek z wykrytymi narzędziami (ffmpeg/whisper/CUDA + tier), persystencja geometrii przez `Config`. Zero hardcodowanych hexów; bez globalnego QSS. „O programie" przez kitowy `HelpWindow` z wymaganym komunikatem prawnym (`LEGAL_BOUNDARIES.md`).
  - **`cli/`** — szkielet Typer (`version`, `info`, `paths`).
  - Testy pytest-qt (offscreen): start okna, round-trip configu przez `Config` z kitu, kolejka zadań (retry), migracje SQLite, detekcja środowiska, strażnik „`core` bez Qt", CLI.

### Security
- Granice z `LEGAL_BOUNDARIES.md` respektowane: brak obchodzenia DRM/TPM i headless-bypassu logowania; sekrety wyłącznie w keyring (nie w configu/repo).
