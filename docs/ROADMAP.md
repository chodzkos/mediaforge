# Roadmap

Kolejność **record-first → import → AI → download-last**. Każdy etap = osobna gałąź `feat/sN-<slug>`, PR do `main`, bramka = kryteria akceptacji spełnione + CI zielone.

Legenda statusu: ☐ todo · ◐ w toku · ☑ done.

---

## ☑ S0 — Fundament
**Gałąź:** `feat/s0-foundation`

Szkielet pakietu (`core`/`gui`/`cli`), `pyproject` + uv, ruff/mypy/pytest, CI (Windows). Config (warstwa nad `Config` z gui-kit), `secrets` (keyring), logowanie, globalna obsługa błędów. Schemat SQLite + migracje. Kolejka zadań (`ThreadPoolExecutor` std-lib + tabela `jobs`, retry; core Qt-free, GUI to adapter z `QTimer`). Powłoka GUI **wpinająca chodzkos-gui-kit** (`ThemeManager` + `attach_titlebar`, dialogi, `LogView`) wg `GUI_STANDARD.md` z repo kitu; górny pasek (logo + motyw + About), status bar.

**Akceptacja:** `uv run mediaforge` otwiera okno z motywem i pustą biblioteką; CLI `--help` działa; pusta kolejka zadań przechodzi smoke-test; CI zielone.

## ☑ S1 — Nagrywanie ekranu i audio (rdzeń produktu)
**Gałąź:** `feat/s1-recorder`

`RecorderEngine`: cały ekran / wybrane okno / region; wybór monitora; FPS i rozdzielczość; **NVENC** (HEVC/AV1); audio: **WASAPI loopback** (dźwięk systemowy) i/lub mikrofon, miks. Presety jakości (Ekonomiczny/Standard/Wysoka/Archiwum/Tylko audio). Pauza/wznowienie. **Crash-safe** zapis długich sesji + segmentacja. Licznik czasu, szacowany rozmiar.

**Akceptacja:** nagranie 1 h zapisane bez utraty przy wymuszonym zamknięciu; A/V zsynchronizowane; tryb tylko-audio działa; pliki trafiają do folderu materiału + wpis w bibliotece.

## ☑ S2 — Import lokalny + biblioteka
**Gałąź:** `feat/s2-library`

`ImporterEngine` (MP4/MKV/MOV/MP3/WAV/M4A…), ekstrakcja audio, miniatury. Biblioteka: lista + metadane (tytuł, data, źródło, prowadzący, organizator, kategoria, tagi, długość, statusy), edycja metadanych, podgląd. Układ „jeden materiał = jeden folder" + `metadata.json`. **Profile źródeł (per domena, definiowane przez użytkownika):** domyślny silnik, metoda logowania, preset jakości, kategoria/tagi, szablon nazwy, tryb notatki, język — dopasowanie po domenie URL. Bez wbudowanego katalogu nazwanych platform.

**Akceptacja:** import pliku tworzy kompletny folder + wpis; biblioteka filtruje po tagach/kategoriach; metadane edytowalne i trwałe; profil źródła wypełnia ustawienia po dopasowaniu domeny.

## ☐ S3 — Transkrypcja
**Gałąź:** `feat/s3-transcribe`

`TranscriptionBackend` (Protocol). Default **whisper.cpp (CUDA)**; alternatywy: faster-whisper (`float16`), insanely-fast-whisper (tor mocy, opcjonalny). **Profil obliczeniowy per maszyna (`core/compute.py`): tier A/B/C wg architektury GPU i VRAM (nie tylko VRAM!) → wybór toru lokalny/chmura + rozmiar modelu Whisper.** Wykrywanie języka (PL/EN), eksport TXT/SRT/VTT, segmenty czasowe, **klikalne timestampy** (skok do momentu), edycja transkryptu, cache. Opcjonalna diaryzacja (`pyannote`) jako osobny etap.

**Akceptacja:** transkrypcja 90-min nagrania PL i EN; klik w tekst przeskakuje odtwarzanie; brak crasha na sm_120 (default whisper.cpp); na słabszej maszynie (Tier B) transkrypcja lokalna z mniejszym modelem; ręczne nadpisanie tieru i backendu działa.

## ☐ S4 — Streszczenia i notatki edukacyjne
**Gałąź:** `feat/s4-summary`

Klient **LiteLLM gateway** (lokal Devstral/Qwen + fallback chmura). **Rejestr dostawców per zadanie (`core/ai/providers.py`):** wybór Claude/ChatGPT/Gemini/DeepSeek osobno dla nazwy / streszczenia / długiego kontekstu / VLM / RAG; klucze per dostawca w keyring; walidacja możliwości (vision/długie okno). Przełącznik **„tylko lokalnie"** per kategoria/materiał. Style: krótkie / dokładne / w punktach / rozdziały. **Tryby dziedzinowe** (medyczny, techniczny, programistyczny, biznesowy, ogólny) — prompt wydobywa definicje, procedury, wzory, przeciwwskazania, pytania kontrolne. Notatka edukacyjna `.md`. **Auto-nazwa pliku** z AI (z sanityzacją pod Windows). Long-context: lokalnie do limitu okna, dłuższe → chmura; hierarchiczne streszczanie tylko gdy konieczne.

**Akceptacja:** 4 style działają; wybór dostawcy per zadanie zapisany i routowany przez LiteLLM; ostrzeżenie przy modelu bez vision dla slajdów; „tylko lokalnie" blokuje wysyłkę do chmury; tryb medyczny generuje sensowną notatkę; materiał 3 h obsłużony (chmura) bez utraty spójności.

## ☐ S5 — Pobieranie bezpośrednie (yt-dlp, bez DRM)
**Gałąź:** `feat/s5-downloader`

`DownloaderEngine` (yt-dlp). Logowanie: `--cookies-from-browser` / ręczne (sekrety w keyring). Wybór jakości z `-F` + presety, tryb audio-only. Łączenie strumieni, osadzanie metadanych/miniatury, pobieranie istniejących napisów. Wykrycie DRM → komunikat z `LEGAL_BOUNDARIES.md` (bez prób obejścia). Przycisk „Aktualizuj yt-dlp". Podpowiedź „pobierz zamiast nagrywać", gdy źródło bez DRM.

**Akceptacja:** pobranie materiału bez DRM z wyborem jakości i audio-only; źródło z DRM zwraca komunikat, nie próbuje obejścia; brak zapisanych sekretów poza keyring.

## ☐ S6 — Slajdy (detekcja + VLM)
**Gałąź:** `feat/s6-slides`

Detekcja zmian klatek (FFmpeg), zrzut unikalnych slajdów. Opis przez **VLM** (lokalnie lub chmura przez LiteLLM) — semantyczny opis schematu/wykresu, nie surowy OCR. Synchronizacja opisów z timestampami transkryptu → `slides.md`. Etap opcjonalny, VRAM sekwencyjnie.

**Akceptacja:** dla wykładu ze slajdami powstaje `slides.md` z opisami i czasami; pipeline nie ładuje VLM równocześnie z LLM.

## ☐ S7 — Wyszukiwanie po bibliotece
**Gałąź:** `feat/s7-search`

FTS5 nad transkryptami i streszczeniami (pełnotekstowe). Następnie embeddingi + lokalna baza wektorowa (LanceDB/Chroma/Qdrant — wybór na tym etapie) → wyszukiwanie semantyczne i pytania do biblioteki (RAG, przez LiteLLM).

**Akceptacja:** zapytanie pełnotekstowe i semantyczne zwraca trafne fragmenty z linkiem do momentu w nagraniu.

## ☐ S8 — Konferencje, eksport, dystrybucja
**Gałąź:** `feat/s8-polish`

Tryb konferencji (segmentacja, harmonogram sesji, raport zbiorczy). Eksport Obsidian/Markdown, fiszki/pytania (Anki/Quizlet JSON/CSV). Powiadomienia Telegram. Packaging (PyInstaller + code signing — uwaga na Defender, jak przy epubQTools). Auto-update yt-dlp/modeli. Pełna polonizacja + dokumentacja użytkownika.

**Akceptacja:** instalator startuje na czystym Windows; raport z konferencji generowany; eksport do Obsidian otwiera się poprawnie.

---

## Kandydat po 1.0 — system pluginów (NIE zaplanowany etap)

Nie budujemy go z góry. Do oceny dopiero gdy **≥3 funkcje z `FEATURES.md` realnie tego wymagają** (osobna instalacja / ciężkie zależności / rozwój out-of-tree). Wtedy: entry points (`importlib.metadata`, grupa `mediaforge.<kategoria>`), bez własnego loadera. Wcześniej modularność realizują rejestry `Protocol` + extras w `pyproject` (patrz `ARCHITECTURE.md` → Punkty rozszerzeń). Decyzja o ekstrakcji wspólnego kodu pluginów do dzielonej paczki — po sprawdzeniu w ≥2 projektach.

## Strategia ekstrakcji do gui-kit

Komponenty GUI sprawdzone tu i w `pdf2md` → kandydaci do `chodzkos-gui-kit` (ekstrakcja po sprawdzeniu w ≥2 projektach).
