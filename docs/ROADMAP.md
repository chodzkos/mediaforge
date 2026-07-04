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

`ImporterEngine` (MP4/MKV/MOV/MP3/WAV/M4A…), ekstrakcja audio, miniatury. Biblioteka: lista + metadane (tytuł, data, źródło, prowadzący, organizator, kategoria, tagi, długość, statusy), edycja metadanych, podgląd. Układ „jeden materiał = jeden folder" + `metadata.json` (źródło prawdy) z **indeksem SQLite odbudowywalnym z folderów** (`rescan` → upsert z każdego `metadata.json`).

**Profile źródeł (per domena) → odroczone do S5.** Domenowe klucze pasują do downloadu/URL-i (S5), nie do importu lokalnego; tabela `source_profiles` została w schemacie. (To NIE to samo co ewentualne lekkie *presety importu* auto-uzupełniające metadane — te są osobną, lżejszą rzeczą, nie domenowymi profilami.)

**Import synchroniczny = świadomy dług → SPŁACONY w S3.** Kopia+FFmpeg blokowały pętlę zdarzeń; od S3 import idzie przez kolejkę `jobs` (kind `import`, wątek roboczy), bez tymczasowego QThread.

**Akceptacja:** import pliku tworzy kompletny folder + wpis; biblioteka filtruje po tagach/kategoriach; metadane edytowalne i trwałe; indeks SQLite odtwarzalny z `metadata.json` (rescan).

> Znana decyzja: wpisy sprzed S2 z `folder=NULL` przeżywają samonaprawę schematu, ale są niewidoczne w bibliotece (filtr `folder IS NOT NULL`) — dane zachowane, do ewentualnego backfillu; nie bug.

## ☑ S3 — Transkrypcja
**Gałąź:** `feat/s3-transcribe`

`TranscriptionBackend` (Protocol). Default **whisper.cpp (CUDA)**; alternatywy: faster-whisper (`float16`), insanely-fast-whisper (tor mocy, opcjonalny). **Profil obliczeniowy per maszyna (`core/compute.py`): tier A/B/C wg architektury GPU i VRAM (nie tylko VRAM!) → wybór toru lokalny/chmura + rozmiar modelu Whisper.** Wykrywanie języka (PL/EN), eksport TXT/SRT/VTT, segmenty czasowe, **klikalne timestampy** (skok do momentu), edycja transkryptu, cache. Opcjonalna diaryzacja (`pyannote`) jako osobny etap.

**Kolejka `jobs` (ThreadPoolExecutor + tabela `jobs` + odświeżanie GUI przez `QTimer`) i przepięcie na nią ZARÓWNO transkrypcji, JAK i importu z S2** — koniec importu synchronicznego (spłata długu z S2). Operacje długie idą przez wątek, GUI pokazuje realny postęp; Stop/anulowanie działa.

> Follow-up domknięty: postęp transkrypcji pokazywany w **procentach** (whisper-cli `--print-progress` → `jobs.progress`), nie tylko status „running". Status-only progres z pierwszej wersji S3 zastąpiony.

**Akceptacja:** transkrypcja 90-min nagrania PL i EN; klik w tekst przeskakuje odtwarzanie; brak crasha na sm_120 (default whisper.cpp); na słabszej maszynie (Tier B) transkrypcja lokalna z mniejszym modelem; ręczne nadpisanie tieru i backendu działa.

## ☑ S4 — Streszczenia (przez gateway LiteLLM, z twardą granicą prywatności)
**Gałąź:** `feat/s4-summaries`

**Dostarczony rdzeń (ten etap):** klient **LiteLLM gateway** jako JEDYNY tor (zero SDK/kluczy dostawców w apce; jedyny sekret = opcjonalny master key gatewaya w keyring). **Routing wrażliwości** (`core/ai/routing.py`): `resolve_route` + `assert_route_allowed` z polityką **domyślnie-lokalne (fail-safe)** — materiał wrażliwy, dopóki `cloud_ok` nie jest jawnie `True` (brak pola = `False`). Streszczenie **na kolejce `jobs`**: model LOKALNY → linia GPU (jeden model w VRAM, dzieli linię z transkrypcją); model CHMUROWY → linia I/O (nie blokuje GPU); linia dobierana przy enqueue wg `resolve_route`. Wyjście `summary.md` w folderze materiału + `summary_status`/`summary_path` (metadata.json + SQLite, podnoszone przez `rescan`). `cloud_ok` jako trwała własność materiału (checkbox w GUI). Podgląd streszczenia (Markdown), przycisk „Streszcz" (aktywny po transkrypcji), `doctor` wypisuje skonfigurowane modele streszczeń.

**Odroczone do follow-upów S4 (nie w tym etapie — szersze funkcje z pierwotnego zakresu):** rejestr dostawców per zadanie w GUI (wybór Claude/ChatGPT/Gemini/DeepSeek per zadanie — model danych `core/ai/providers.py` istnieje), style (krótkie/dokładne/w punktach/rozdziały), **tryby dziedzinowe** (medyczny/techniczny/…), **auto-nazwa pliku** z AI, long-context z hierarchicznym streszczaniem. Wracają jako osobne, mniejsze gałęzie — rdzeń (gateway-only + granica prywatności + kolejka) jest fundamentem, na którym siadają.

**Akceptacja (rdzeń):** streszczenie lokalne i chmurowe routowane przez gateway; **„bez zgody = wyłącznie lokalnie"** egzekwowane dwustopniowo (resolve wybiera, assert blokuje wrażliwy→chmura); padnięty gateway → czytelny błąd z URL-em; streszczenie lokalne serializowane z transkrypcją na linii GPU, chmurowe na I/O. Bramka zielona (ruff + mypy --strict + pytest).

## ☐ S5 — Pobieranie bezpośrednie (yt-dlp, bez DRM)
**Gałąź:** `feat/s5-downloader`

`DownloaderEngine` (yt-dlp). Logowanie: `--cookies-from-browser` / ręczne (sekrety w keyring). Wybór jakości z `-F` + presety, tryb audio-only. Łączenie strumieni, osadzanie metadanych/miniatury, pobieranie istniejących napisów. Wykrycie DRM → komunikat z `LEGAL_BOUNDARIES.md` (bez prób obejścia). Przycisk „Aktualizuj yt-dlp". Podpowiedź „pobierz zamiast nagrywać", gdy źródło bez DRM.

**Profile źródeł (per domena, definiowane przez użytkownika) — przeniesione z S2:** domyślny silnik, metoda logowania, preset jakości, kategoria/tagi, szablon nazwy, tryb notatki, język — dopasowanie po domenie URL. Bez wbudowanego katalogu nazwanych platform. Tabela `source_profiles` istnieje od S2; tu dochodzi wiring. (To domenowe profile dla URL-i — odrębne od ewentualnych lekkich *presetów importu* metadanych.)

**Akceptacja:** pobranie materiału bez DRM z wyborem jakości i audio-only; źródło z DRM zwraca komunikat, nie próbuje obejścia; brak zapisanych sekretów poza keyring; profil źródła wypełnia ustawienia po dopasowaniu domeny (przeniesione z S2).

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

---

## Backlog / Odroczone

Funkcje i ulepszenia świadomie odłożone — z powodem odroczenia i warunkiem, przy którym wracają. Nie planowane etapy; do wyciągnięcia, gdy potrzeba stanie się realna.

### Rekorder — architektura (przyszłość)

**Ciepły pipeline nagrywania (OBS-style).**
Problem: ddagrab startuje na zimno przy każdym nagraniu; Desktop Duplication API łapie stabilny strumień dopiero po ~3 s, pierwsze nierówne klatki trafiają do pliku jako szarpanie na starcie. Potwierdzone pomiarami: niezależne od rozdzielczości (1080p i 2560×1600 mają identyczną głowę) i od CPU (wyrabia) — timing źródła (DDA), nie przepustowość. `drop=` narasta tylko przez ~4 s, potem stoi; środek płynny. Długość pliku poprawna (15 s → 15.000 s), więc to nie problem sync. Workaround (`fix/recorder-av-sync`): pre-roll to WYŁĄCZNIE odczekanie w GUI („Przygotowuję…") — głowa zostaje w pliku, ale przed treścią. Cięcie `trim`-em świadomie odrzucone: asymetryczne (samo wideo) rozjeżdża A/V, a symetryczne (`atrim` na audio) to dodatkowa kruchość w filtrze przy znikomym zysku (głowa i tak jest przed treścią) — **usunięcie transientu u źródła to zadanie ciepłego pipeline'u**, nie łatania filtrem. Docelowe: trzymać capture+enkoder ciągle rozgrzany (jak OBS), zapis włączać na żądanie bez restartu ffmpeg → zero transientu, natychmiastowy start, brak pre-rollu. Koszt: długo żyjący proces ffmpeg w tle, sterowanie zapisem (segmentacja / named pipe / API), zarządzanie GPU w stanie gotowości, integracja z kolejką jobs i cyklem życia GUI.

**Pełen-GPU pipeline nagrywania (`hwmap` → `scale_cuda` → `nvenc`).**
Obecny pipeline: `hwdownload` → konwersja yuv420p na CPU → nvenc (GPU→RAM→CPU→GPU per klatka). Pełen-GPU wyeliminowałby transfery. ZABLOKOWANE na tym laptopie przez Optimus: ekran na iGPU (Intel), CUDA na dGPU (RTX) → `hwmap` D3D11→CUDA daje `Failed to create derived device context: -40`. Odblokowywalne tylko trybem tylko-dGPU (BIOS/MUX) — wtedy D3D11 i CUDA na jednej karcie. Uwaga: przy obecnej rozdzielczości/fps CPU wyrabia (steady-state płynny), więc to optymalizacja, nie konieczność — sensowne dopiero przy wyższej rozdzielczości/fps lub maszynie bez hybrydy.

**Driver-free WASAPI loopback (dźwięk systemowy bez VB-Cable).**
ffmpeg na Windows nie ma natywnego WASAPI; dźwięk systemowy wymaga urządzenia loopback dshow (Stereo Mix / wirtualny kabel), które użytkownik musi zainstalować/włączyć. Docelowe: pythonowe przechwytywanie WASAPI loopback (`pyaudiowpatch` albo `soundcard`) → PCM na stdin ffmpeg, bez VB-Cable. Koszt: nowy komponent + wątek + ffmpeg przestaje być jedyną ścieżką audio. Powiązane: `feat/dshow-device-enum` (enum + detekcja loopbacku + hint) łagodzi, ale nie tworzy urządzenia.

### Rekorder — znane ograniczenia

**Capture okna po tytule — niewspierane.**
ddagrab łapie cały monitor, nie zna okien. Capture okna po tytule usunięty z GUI (`feat/recorder-region-crop`); zostały monitor (`output_idx`) + region (`crop`). Przywrócenie wymagałoby innego backendu (np. Windows.Graphics.Capture per-okno) — realna zmiana. Region-crop pokrywa większość potrzeb (wytnij obszar odtwarzacza).

**Doklejanie do istniejącego nagrania (append).**
Przy kolizji nazwy (`fix/recorder-name-collision`) dajemy Nadpisz / Nową nazwę / Anuluj — **bez** opcji „doklej". Doklejanie do gotowego materiału wymaga wymuszenia identycznych parametrów sesji (rozdzielczość/fps/enkoder) i concat świadomego timestampów; przy segmentowej mechanice i concat kopią strumieni append o innych parametrach = plik uszkodzony/nieodtwarzalny od połowy. Rozważyć przy ciepłym pipeline (stały, rozgrzany capture+enkoder o znanych parametrach ułatwia bezpieczne dopisywanie).

### Streszczenia / AI

**Bezpośrednie API dostawców (fallback bez gatewaya).**
Obecnie twarda granica: wyłącznie gateway LiteLLM (jeden punkt egzekwowania routingu wrażliwości i przechowywania kluczy). Gdyby operacyjnie przeszkadzało (gateway musi chodzić, żeby były podsumowania), dorobić bezpośredni tor API (anthropic/openai/gemini/deepseek — sondy providerów w doctorze już są) POD WARUNKAMI: routing wrażliwości (`resolve_route` + `assert_route_allowed`) egzekwowany identycznie w torze bezpośrednim; klucze w keyring; wspólny `Protocol` dla obu torów, żeby handler nie wiedział, którym idzie. Bez tych warunków bezpośredni tor rozmywa gwarancję prywatności — nie robić „na szybko".

**Rejestr dostawców per zadanie w GUI + style / tryby dziedzinowe / auto-nazwa (follow-upy S4).**
Rdzeń S4 dostarczył tor gateway + granicę prywatności + kolejkę. Do dorobienia jako osobne gałęzie: wybór modelu per zadanie w ustawieniach (model danych `core/ai/providers.py` istnieje, walidacja vision też), style streszczeń (krótkie/dokładne/w punktach/rozdziały), tryby dziedzinowe (medyczny/techniczny/programistyczny/biznesowy/ogólny), auto-nazwa pliku z AI (sanityzacja pod Windows), long-context z hierarchicznym streszczaniem (lokalnie do limitu okna, dłuższe → chmura). Świadomie odłożone, żeby najpierw utwardzić granicę prywatności i tor kolejki.

### Transkrypcja

**Backend HF / insanely-fast-whisper (tor torch).**
Przy S3 zaimplementowano tylko whisper.cpp (torch-free, działa na Blackwell). Protocol backendu + stub zostawione pod drugi tor (torch/transformers) dla jakości/prędkości na niektórych kartach. Odroczone, bo torch na Blackwell sm_120 to osobny temat (crash bez odpowiedniej wersji). Sensowne, gdy whisper.cpp okaże się niewystarczający albo torch+Blackwell się ustabilizuje.

**Pełne strumieniowanie linii transkrypcji.**
`feat/transcribe-progress` pokazuje procent (`jobs.progress` + polling). Strumieniowanie surowych linii whisper-cli na żywo (segmenty tekstu w trakcie) wymaga osobnej infrastruktury log-per-job. Procent rozwiązuje „czy stoi?"; strumień linii to wygoda, nie konieczność.

### Biblioteka / dane

**Wiele library roots.**
Obecnie system zna jeden kanoniczny root (`default_recordings_dir()`): rescan skanuje tylko jego, delete/path-safety działają tylko w jego obrębie. Materiały świadomie zapisane poza (rekorder na to pozwala, z ostrzeżeniem od `fix/out-of-library-warning`) są nieskanowane i niezarządzane. Docelowo: konfigurowalna LISTA rootów — rescan iteruje po wszystkich, path-safety = `is_relative_to(dowolny root)`, delete działa w każdym, GUI pozwala dodać/usunąć root w ustawieniach. Zrobić, gdy biblioteka realnie urośnie poza jeden katalog (np. drugi dysk / NAS jako osobny root). Uwaga wdrożeniowa: klucz konfiguracyjny od razu jako lista (`library_roots: [ścieżka]`) z migracją z pojedynczego — uniknie drugiej migracji configu.

**Rescan raportuje pominięty prune do status baru.**
Guard NAS-safety pomija prune, gdy root niedostępny/pusty a indeks niepusty (żeby QNAP offline nie wyczyścił biblioteki). Efekt uboczny: gdy legalnie opróżnisz bibliotekę, „Przeskanuj" nie czyści indeksu i nie mówi dlaczego — wygląda jak bug. Rescan powinien zwracać liczbę pominiętych/usuniętych i pokazywać w status barze („Pominięto prune: root pusty lub niedostępny"). Drobne, UX.

**Backfill wierszy pre-S2 (`folder=NULL`).**
`ensure_schema` migruje stare bazy (dodaje `folder`/`presenter`/…), ale wiersze sprzed S2 mają `folder=NULL` i nie pokazują się w `list_materials()` (filtr prawdziwych materiałów). Dane nie giną. Jeśli takie wiersze mają foldery z `metadata.json` — rescan powinien je naprawić (upsert z folderem), sprawdzić brak duplikatów. Jeśli nie mają (format pre-S2) — backfill albo poluzowanie filtra. Dotyczy tylko realnych nagrań sprzed S2.

### Layout/kandydat do gui-kit

**DetailPanel.**
„DetailPanel: wzorzec panelu szczegółów odpornego na elementy zmiennej wysokości — wynieść z mediaforge po sprawdzeniu w S5/S6, gdy pojawi się drugi konsument".
