# Prompty dla Claude Code

Gotowe do wklejenia, po jednym na etap. Każdy zakłada, że Claude Code najpierw czyta `docs/CLAUDE.md`, `docs/ARCHITECTURE.md` i `docs/LEGAL_BOUNDARIES.md`. Pracuj na gałęzi etapu, commituj w stylu Conventional Commits, na końcu zaktualizuj `CHANGELOG.md` i status w `ROADMAP.md`.

---

## S0 — Fundament

```
Przeczytaj docs/CLAUDE.md, docs/ARCHITECTURE.md, docs/LEGAL_BOUNDARIES.md.
Pracuj na gałęzi feat/s0-foundation.

Zbuduj fundament pakietu mediaforge:
0. `uv sync` (pociąga chodzkos-gui-kit z gita, pin do taga v0.5.0 — jest w pyproject).
1. Layout src/mediaforge/{core,gui,cli} zgodny z ARCHITECTURE.md; core BEZ importów Qt.
2. core/config.py: JEST w repo jako cienka warstwa nad chodzkos_gui_kit.config.Config —
   dodaj typowane akcesory dla kluczy mediaforge (motyw, ostatnie katalogi, profil obliczeniowy
   per maszyna, rejestr dostawców per zadanie). Profile źródeł NIE tu — to dane relacyjne,
   idą do tabeli SQLite source_profiles (pkt 5). NIE reimplementuj magazynu/platformdirs. Debounce po
   stronie GUI (on_dirty → QTimer → flush()).
3. core/secrets.py: JEST w repo (keyring). W razie potrzeby rozszerz o nazewnictwo kluczy per dostawca.
4. core/jobs/: kolejka na `ThreadPoolExecutor` (std-lib) + tabela SQLite `jobs` (status, progress,
   error_message, retry). core BEZ Qt — QThread łamie regułę i nie działa w CLI (brak pętli Qt);
   GUI to adapter odświeżający widok przez QTimer. BEZ Celery/Redis. Workery nie dotykają obiektów Qt.
5. core/library/: schemat SQLite (recordings, jobs, transcripts, summaries, tags,
   source_profiles) + lekkie migracje (user_version). BEZ tabeli settings — preferencje skalarne
   (motyw, ostatnie katalogi, profil obliczeniowy, rejestr dostawców) trzyma config.json (config.py).
6. gui/: powłoka okna WPINAJĄCA chodzkos-gui-kit — NIE pisz własnego theme.py/dialogów:
   - `ThemeManager(app, cfg)` + `apply("auto")` + `attach_titlebar(window)`;
   - górny pasek wg GUI_STANDARD §6: logo + przełącznik motywu (auto/jasny/ciemny) + About;
   - status bar (wykryte narzędzia: ffmpeg/whisper/CUDA + tier z core/compute);
   - LogView z gui-kit do statusu; persystencja geometrii okna przez Config.
   Zero hardcodowanych hexów; nie nadstylizowuj generycznych QToolButton/QLineEdit (przeciek do dialogów).
7. cli/: szkielet Typer z `--help`.
8. Globalna obsługa wyjątków + logowanie do pliku w katalogu logów (config_dir z kitu / platformdirs).
9. Testy pytest-qt (offscreen) dla startu okna i config round-trip (przez Config z kitu).

Definicja ukończenia: ruff + mypy --strict + pytest zielone; CI przechodzi.
```

## Doctor — diagnostyka środowiska (przyrost po S0)

```
Gałąź feat/doctor. UWAGA: S0 jest już zrobione — nie przebudowuj go.

Pakiet core/detection/ JEST w repo (hardware.py + tools.py + report.py; wzorzec z pdf2md,
Qt-free, checki odporne, detection.check_all()). Jest też test_detection.py. Struktura lustruje
pdf2md pod przyszłe `git mv detection/ → chodzkos-detection`. Twoje zadanie to WPIĘCIE i konsolidacja:

1. CLI: dodaj komendę `doctor` do istniejącego cli/main.py (NIE nadpisuj reszty CLI z S0).
   Override (whisper.cpp, LiteLLM) czytaj z core/config i PODAJ do check_all — detection/ jest
   celowo odsprzężone od configu:

       @app.command()
       def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
           "Sprawdź dostępność narzędzi i zasobów (ffmpeg, whisper.cpp, GPU, LiteLLM)."
           from mediaforge.core import config, detection
           cfg = config.load()
           report = detection.check_all(
               whispercpp_path=cfg.get("whispercpp_path"),
               litellm_base_url=cfg.get("litellm_base_url"),
           )
           if as_json:
               import json
               typer.echo(json.dumps(report, indent=2, ensure_ascii=False, default=str))
           else:
               typer.echo(detection.render_report(report))

   (render_report JEST w report.py — prezentacja oddzielona od sond; GUI używa check_all() wprost.
   default=str w json bo path to obiekt Path.)
2. Status bar GUI: jeśli S0 dodał JAKĄKOLWIEK doraźną detekcję narzędzi/tieru, ZASTĄP ją
   odczytem z detection.check_all() — jedno źródło prawdy, bez dwóch ścieżek wykrywania.
3. Klucze configu: `whispercpp_path` (binarka whisper.cpp poza PATH — override) i `litellm_base_url`.
   Dodaj typowane akcesory w core/config; sonda używa override → fallback shutil.which.
4. Nie dubluj detekcji GPU przez torch — zostaje nvidia-smi; sonda torcha to dodatek (już tak jest).
   Nowe narzędzia sonduj przez tools.probe_tool (kontrakt {available, version, path}), nie goły which.

Definicja ukończenia: `mediaforge-cli doctor` zwraca raport bez crasha gdy narzędzi brak;
wykryty GPU mapuje się na poprawny tier; override whisper.cpp znajduje binarkę poza PATH;
status bar pokazuje to samo co doctor; testy zielone.
```

## S1 — Nagrywanie ekranu i audio

```
Gałąź feat/s1-recorder. Przeczytaj CLAUDE.md (sekcja pułapek: NVENC, WASAPI, FFmpeg subprocess).

Zaimplementuj RecorderEngine wg interfejsu AcquisitionEngine:
- źródła: cały ekran / wybrane okno / region; wybór monitora (multi-monitor, DPI-aware).
- wideo: FFmpeg subprocess, enkoder NVENC (HEVC/AV1) z fallbackiem programowym; FPS i rozdzielczość.
- audio: WASAPI loopback (dźwięk systemowy) i/lub mikrofon; opcja miksowania.
- presety jakości z ARCHITECTURE/ROADMAP (Ekonomiczny/Standard/Wysoka/Archiwum/Tylko audio).
- pauza/wznowienie; crash-safe długie nagrania (segmentacja + zapis przyrostowy);
  licznik czasu i szacowany rozmiar w GUI.
- po zakończeniu: utworzenie folderu materiału + wpis w bibliotece (status: nagrane).
- GUI: użyj `LogView` z gui-kit (parametr `level_colors` dla statusów nagrywania, np. `{"recording": ...}`) i `PathEntry` (katalog wyjściowy). Bez własnych widgetów ścieżki/logu.

Testy: jednostkowe budowania komend FFmpeg; test odporności (symulacja przerwania) — plik
częściowy musi być odzyskiwalny. Definicja ukończenia jak w CLAUDE.md.
```

## S2 — Import lokalny + biblioteka

```
Gałąź feat/s2-library.

1. ImporterEngine: import MP4/MKV/MOV/MP3/WAV/M4A; ekstrakcja audio (FFmpeg); miniatury.
   GUI importu: `FileList` z gui-kit (ma drag&drop + toolbar Dodaj/Usuń/Wyczyść) — nie pisz własnej listy.
2. Biblioteka GUI: lista materiałów z metadanymi (tytuł, data, źródło, prowadzący,
   organizator, kategoria, tagi, długość, statusy transkrypcji/streszczenia), edycja metadanych,
   filtrowanie po tagach/kategoriach, podgląd.
3. Utrwalenie układu "jeden materiał = jeden folder" + metadata.json jako źródło prawdy
   synchronizowane z SQLite.

Testy round-trip metadanych (folder <-> SQLite). Definicja ukończenia jak w CLAUDE.md.
```

## S3 — Transkrypcja

```
Gałąź feat/s3-transcribe. UWAGA na sekcję Blackwell w CLAUDE.md.

1. core/ai/transcribe.py: Protocol TranscriptionBackend + implementacje:
   - whispercpp (DEFAULT, build CUDA, subprocess) — bez torcha/CTranslate2/transformers.
   - faster_whisper z compute_type="float16" (NIGDY domyślny int8 na sm_120).
   - insanely_fast_whisper (opcjonalny extra "transcribe-hf"; udokumentuj nightly torch + piny transformers).
   - cloud (przez LiteLLM) jako fallback.
2. Auto-detekcja CUDA/VRAM przy starcie → wybór toru lokalnego lub chmury; ręczne nadpisanie w ustawieniach.
3. Wykrywanie języka (PL/EN), eksport TXT/SRT/VTT, segmenty czasowe, cache wyników.
4. GUI: panel transkryptu z klikalnymi timestampami (skok odtwarzania), edycja, wyszukiwanie w tekście. Streaming postępu przez `LogView` z gui-kit (`level_colors={"transcribing": ...}`).
5. core/ai/diarize.py: opcjonalna diaryzacja pyannote jako osobny etap; token HF z keyring; łączenie
   mówców z segmentami transkryptu.

Testy: budowanie wywołań backendów (mock), parsowanie segmentów -> SRT/VTT. Definicja ukończenia jak w CLAUDE.md.
```

## S4 — Streszczenia i notatki edukacyjne

```
Gałąź feat/s4-summary.

1. core/ai/summarize.py: klient LiteLLM gateway (httpx). NIE stawiaj vLLM/własnego serwera.
   Konfiguracja endpointu + model lokalny/chmurowy; klucze przez keyring.
2. Style: krótkie / dokładne / w punktach / rozdziały. Tryby dziedzinowe (medyczny, techniczny,
   programistyczny, biznesowy, ogólny) jako szablony promptów wydobywające definicje, procedury,
   wzory, przeciwwskazania, pytania kontrolne.
3. Notatka edukacyjna .md wg szablonu z dokumentacji rozmowy (tytuł, opis, tematy, definicje,
   procedury, przykłady, pytania kontrolne, fragmenty do powtórki, linki czasowe).
4. core/naming.py: propozycja nazwy pliku z AI + sanityzacja pod Windows (reserved chars, długość).
5. Long-context: jeśli transkrypt mieści się w oknie -> jeden przebieg; jeśli nie -> hierarchiczne
   streszczanie sekcyjne LUB model chmurowy o dużym oknie (przez LiteLLM). NIE prymitywny chunking domyślnie.

Testy: sanityzacja nazw, wybór trybu long-context vs hierarchiczny wg długości. Definicja ukończenia jak w CLAUDE.md.
```

## S5 — Pobieranie bezpośrednie (yt-dlp)

```
Gałąź feat/s5-downloader. OBOWIĄZKOWO przeczytaj LEGAL_BOUNDARIES.md.

1. DownloaderEngine (yt-dlp jako biblioteka). Logowanie WYŁĄCZNIE przez --cookies-from-browser
   lub ręczne; sekrety w keyring. ZAKAZ: headless scraping, automatyczne omijanie 2FA, jakiekolwiek
   obchodzenie DRM/TPM.
2. Wybór jakości (yt-dlp -F) + presety + audio-only; łączenie strumieni; osadzanie metadanych i
   miniatury; pobieranie istniejących napisów.
3. Wykrycie zabezpieczeń/DRM -> komunikat z LEGAL_BOUNDARIES.md, BEZ prób obejścia.
4. Przycisk "Aktualizuj yt-dlp"; luźny pin wersji. Podpowiedź "pobierz zamiast nagrywać" gdy źródło bez DRM.

Jeśli jakiekolwiek wymaganie wymusza obejście zabezpieczeń — ZATRZYMAJ SIĘ i poproś o potwierdzenie zakresu.
Definicja ukończenia jak w CLAUDE.md.
```

## S6 — Slajdy (VLM)

```
Gałąź feat/s6-slides.

1. Detekcja zmian klatek (FFmpeg scene/select) -> zrzut unikalnych slajdów.
2. core/ai/slides.py: opis slajdów przez VLM (lokalnie lub chmura przez LiteLLM) — opis semantyczny
   (schemat/wykres), nie surowy OCR. VRAM sekwencyjnie (zwolnij Whisper/LLM przed VLM).
3. Synchronizacja opisów z timestampami transkryptu -> slides.md.

Etap opcjonalny (flaga w ustawieniach). Definicja ukończenia jak w CLAUDE.md.
```

## S7 — Wyszukiwanie

```
Gałąź feat/s7-search.

1. FTS5 nad transcripts.text i summaries.content; UI wyszukiwania pełnotekstowego z linkiem do momentu.
2. Embeddingi + lokalna baza wektorowa (porównaj LanceDB/Chroma/Qdrant, wybierz i uzasadnij w docs).
3. Wyszukiwanie semantyczne + pytania do biblioteki (RAG przez LiteLLM).

Definicja ukończenia jak w CLAUDE.md.
```

## S8 — Konferencje, eksport, dystrybucja

```
Gałąź feat/s8-polish.

1. Tryb konferencji: segmentacja nagrania, harmonogram sesji, automatyczne nazwy wg godzin, raport zbiorczy.
2. Eksport Obsidian/Markdown; fiszki/pytania (Anki/Quizlet JSON/CSV).
3. Powiadomienia Telegram po zakończeniu zadań (token w keyring).
4. Packaging PyInstaller + code signing (uwaga na Windows Defender — patrz doświadczenia z epubQTools).
   Auto-update yt-dlp/modeli. Pełna polonizacja + dokumentacja użytkownika jako okno pomocy przez
   `HelpWindow` z gui-kit (zakładki `(tytuł, html)` składane helperami `help_html` — zero hexów).

Definicja ukończenia jak w CLAUDE.md.
```
