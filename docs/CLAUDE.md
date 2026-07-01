# Brief dla Claude Code

Czytaj ten plik przed każdym etapem. Zawiera konwencje, pułapki i twarde granice.

## Konwencje

- Python 3.12, pakiet w `src/mediaforge/`, zarządzanie przez **uv**.
- **ruff** (lint + format), **mypy --strict**, **pytest** + **pytest-qt**. Wszystko musi przechodzić przed PR.
- **Conventional Commits** (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- Gałąź na etap: `feat/sN-<slug>`, PR do `main`, stage gate wg `ROADMAP.md`.
- Dokumentacja i komentarze po polsku; identyfikatory w kodzie po angielsku.
- **Układ repo:** dokumentacja prozą → `docs/`. W rootcie zostają wyłącznie pliki wymagane przez narzędzia/konwencję: `README.md`, `LICENSE`, `pyproject.toml`, `.gitignore`, `.github/` oraz `CHANGELOG.md`. Nie przenoś tych plików do `docs/` (psuje wykrywanie przez GitHub/build).
- **Rdzeń (`core/`) nie importuje Qt.** GUI i CLI są cienkimi adapterami nad `core`.
- Config przez **`Config` z chodzkos-gui-kit** (owinięte w `core/config.py`), zapis atomowy; **debounce ~1 s realizuje GUI** (`on_dirty` → `QTimer` → `flush()`). Nie reimplementuj magazynu.
- Sekrety wyłącznie przez `keyring`. Nigdy nie loguj cookies/haseł/tokenów.

## Twarde granice bezpieczeństwa (NIE negocjowalne)

1. **Nie implementuj obchodzenia DRM/TPM** — żadnego odszyfrowywania strumieni, zrzutu kluczy, dekrypcji Widevine/PlayReady.
2. **Nie implementuj headless scrapingu omijającego logowanie/2FA** — odrzuć pomysł „Playwright Persistent Context w trybie headless do pobierania chronionego strumienia". Logowanie = `cookies-from-browser` lub ręczne logowanie użytkownika; przeglądarka (jeśli w ogóle) działa widocznie i steruje nią użytkownik, nie bot.
3. Status DRM = „do ustalenia później" oznacza decyzję o zakresie **legalnym**, nie o obejściu zabezpieczeń.
4. Przy zadaniu dotykającym tych obszarów — **zatrzymaj się i poproś o potwierdzenie zakresu.** Patrz `LEGAL_BOUNDARIES.md`.

## Pułapki techniczne (sprzęt: RTX 5090 mobile, 24 GB VRAM, Blackwell sm_120, 128 GB RAM, Windows)

- **24 GB to VRAM, nie 128 GB RAM.** O lokalnym LLM/Whisper/VLM decyduje VRAM. Nie zakładaj, że duży RAM pozwala na pełny long-context lokalnie — długi kontekst rośnie przez KV-cache w VRAM. Realny lokalny kontekst ~30–40k tokenów na ~24B; dłuższe → chmura przez LiteLLM.
- **CTranslate2/faster-whisper na sm_120:** domyślny `int8` rzuca `CUBLAS_STATUS_NOT_SUPPORTED`. Jeśli używasz faster-whisper — wymuś `compute_type="float16"`.
- **Domyślny backend transkrypcji = whisper.cpp (CUDA).** Brak PyTorcha/CTranslate2/transformers = brak całej klasy problemów sm_120. Tor HF (`insanely-fast-whisper`, `pyannote`) jest opcjonalny i wymaga nightly PyTorcha (cu12x); pamiętaj o pinach `transformers` (znana bolączka z `pdf2md`: Marker/Docling) i o tym, że Flash Attention na Blackwell/Windows bywa niedostępny — fallback do SDPA/FA2.
- **pyannote** = gated model HF → wymaga tokenu w `keyring`/`.env` (nie commituj).
- **VRAM sekwencyjnie:** nie ładuj Whispera, VLM i LLM równocześnie. Każdy etap zwalnia GPU przed następnym.
- **FFmpeg jako subprocess**, nie biblioteka linkowana — utrzymuje licencję MIT i izoluje crash. NVENC do enkodowania (HEVC/AV1). Dźwięk systemowy przez urządzenie DirectShow zdolne do loopbacku (Stereo Mix / wirtualny kabel np. VB-Cable); ffmpeg nie ma natywnego wejścia WASAPI. Driver-free WASAPI loopback wymagałby osobnego komponentu (pyaudiowpatch/soundcard → PCM na stdin ffmpeg) — świadoma decyzja na S1.x.
- **Nagrywanie z dwóch wejść na żywo (ddagrab + dshow):** ZAWSZE `-use_wallclock_as_timestamps 1` na OBU wejściach + `-audio_buffer_size 50` na dshow + `aresample=async=1` na audio. Bez tego: ~500 ms paczki audio → non-monotonic DTS → ciągłe dropy wideo i rozjazd A/V. **NIE tnij `trim`-em samego wideo** (asymetryczne cięcie rozjeżdża A/V; głowę zimnego startu ddagrab maskuje pre-roll w GUI — odczekanie, nie ffmpeg).
- **yt-dlp** zmienia się szybko — luźny pin + opcja „Aktualizuj yt-dlp". Etap S5, nie wcześniej.
- **Nie wprowadzaj** Celery/Redis/FastAPI/Tauri/Electron/vLLM. Kolejka = **`ThreadPoolExecutor` (std-lib) + tabela `jobs`** w core (Qt-free — QThread wymaga pętli Qt i łamie regułę „core bez Qt", a CLI nie miałoby pętli); GUI to adapter odświeżający widok przez `QTimer`. Streszczenia = istniejący gateway LiteLLM.
- **PySide6, nie PyQt6** (PyQt6 jest GPL; przy MIT używamy PySide6/LGPL). **GUI stoi na `chodzkos-gui-kit`** — NIE pisz własnego `theme.py` ani dialogów. Używaj `ThemeManager` (`apply`/`attach_titlebar`), `qt.dialogs.*`, widgetów `PathEntry`/`FileList`/`LogView`/`HelpWindow` (+ helpery `help_html`), `get_icon`/`ICON_MAP`. `GUI_STANDARD.md` mieszka w repo kitu (nie kopiuj go tutaj). Pin do **taga** (`v0.5.0`); podniesienie wersji = osobny commit `chore:` + testy. Szczegóły: `ARCHITECTURE.md` → Integracja z chodzkos-gui-kit.

## Anti-amnesia (GUI)

Każdy nowy ekran/dialog GUI: używaj `ThemeManager` i widgetów z gui-kit (`PathEntry`/`FileList`/`LogView`) zamiast pisać od zera; **nie hardcoduj kolorów** (role/paleta kitu — hexy żyją tylko w `palette.py` kitu); **nie nadstylizowuj generycznych typów** (`QToolButton`/`QLineEdit`/`QComboBox`) globalnym QSS (przecieka do dialogów kitu — stylizuj per-widget); strumień operacji długich przez `LogView` (parametr `level_colors` dla statusów nagrywania/transkrypcji); dodawaj tooltipy; podpinaj akcje (żadnych martwych metod bez bindingu); obsługuj Stop/anulowanie; persystuj geometrię okna i ostatnie katalogi (przez `Config` z kitu). Nowe ikony mediaforge (record/mic/download/captions) → najpierw lokalnie, potem PR do gui-kit (reguła ekstrakcji).

## Definicja ukończenia etapu

ruff + mypy --strict + pytest zielone · zaktualizowany CHANGELOG · zaktualizowany ROADMAP (oznaczony etap) · brak naruszeń granic z `LEGAL_BOUNDARIES.md`.

## Punkty rozszerzeń i pluginy

- **Teraz (rób od razu):** modularność przez **rejestry oparte na `Protocol`** (jak `AcquisitionEngine`, `TranscriptionBackend`). Nowe kategorie rozszerzalne (eksportery, tryby notatek, style streszczeń, kanały powiadomień, VLM/tłumaczenie) projektuj jako rejestr + Protocol. Opcjonalne ciężkie zależności → **extras w pyproject**, nie własny mechanizm.
- **NIE buduj** pełnego systemu pluginów (osobne paczki, instalacja, włączanie w UI) na tym etapie — to over-engineering dla aplikacji jednoosobowej i zaprojektujesz złe API, zanim powstaną 3–4 realne pluginy.
- **Później (po 1.0, jeśli ≥3 funkcje tego chcą):** zewnętrzne pluginy przez **entry points** (`importlib.metadata`, grupa `mediaforge.<kategoria>`), nigdy własny loader. Konwencję grup standaryzujemy między projektami; wspólny kod (mini `Registry` + loader) wyodrębniamy do wspólnej paczki dopiero po sprawdzeniu w ≥2 projektach (pdf2md + mediaforge).

## Diagnostyka (doctor) — jedno źródło detekcji

- Detekcja narzędzi/GPU = wyłącznie pakiet `core/detection/` (`detection.check_all()`; struktura: `hardware`/`tools`/`report`). **Nie duplikuj** wykrywania ad-hoc w GUI — status bar i komenda `doctor` czytają to samo `check_all()`. Jeśli S0 dodał doraźną detekcję do status bara, skonsoliduj ją tutaj.
- **Kontrakt `probe_tool` = nadzbiór pakietowego `chodzkos-detection.probe_tool`**: `{available, version}` (zgodne nazwy) + `path` (rozszerzenie). Nowe narzędzia sonduj przez `probe_tool` (NIE goły `shutil.which` rozsiany po kodzie). Gdy pakiet dostanie `_make_tool` (path + fallback katalogów), podmień ciało `probe_tool` na import z pakietu — reszta bez zmian. NIE kopiuj `_make_tool` EpubForge teraz.
- **whisper.cpp przez override `whispercpp_path` z configu** → fallback `shutil.which` (binarka bywa self-compiled poza PATH). Override przeżyje migrację; nie rób własnego fallbacku na katalogi. Override (whisper.cpp) i `litellm_base_url` przekazuj do `check_all(...)` z wiringu (CLI/GUI czytają config), żeby `detection/` było odsprzężone od configu.
- **Trzymaj trzy warstwy rozdzielone** (nie splataj jak pdf2md `doctor()`): prezentacja (`render_report`, operuje tylko na danych) ≠ sondy uniwersalne (`command_in_path`/`api_key_present`/`check_gpu`) ≠ definicje „co sprawdzać" (ffmpeg/whisper.cpp — specyfik mediaforge). `check_all()` zwraca **dane**, render jest osobny.
- **Cel ekstrakcji to osobny pakiet `chodzkos-detection` (Qt-free, stdlib-only), NIE gui-kit** (gui-kit ciągnie Qt; detekcja musi być instalowalna bez Qt). Do pakietu: sondy uniwersalne (hardware nvidia-smi + prymitywy `command_in_path`/`api_key_present`). **Render zostaje app-side** (pdf2md: Rich, mediaforge: Qt). Polityka tierów (`compute.classify`) zostaje w mediaforge. Nie twórz pakietu teraz — wydziel (`git mv detection/ → chodzkos-detection`), gdy doctor działa na pdf2md i mediaforge.
- **Kontrakt `HardwareInfo` przy wydzieleniu:** zachowaj nazwy istniejących pól; **nowy sygnał = nowe pole, nie przeróbka** (inaczej rozjedzie się pdf2md). `detect_hardware()` uniwersalne; „używalność" (`whisper_cuda_ok` mediaforge / `torch_cuda_ok` pdf2md) jest warstwą w aplikacji.
- **NIE przenoś progu sm_75 z pdf2md** (to próg cu130/torch). whisper.cpp/ggml schodzi niżej — GTX 1070 (Pascal) może działać, więc jest w Tier B z `transcription_local=True`. Realny sygnał da sonda `whisper_cuda_ok` w S3 z własnym progiem; do tego czasu heurystyka arch+VRAM (placeholder).
- **GPU przez `nvidia-smi`, nie torch** (domyślne ścieżki torch-free). Sonda torcha tylko jako dodatek, gdy torch obecny. Sonda nvidia-smi musi być odporna: gate `command_in_path`, sprawdzenie `returncode`, `compute_cap` osobnym zapytaniem z **fallbackiem arch z nazwy GPU** (starsze sterowniki nie mają `compute_cap`).
- **Granica ekstrakcji GPU:** `check_gpu()` zwraca surowe fakty (→ `chodzkos-detection`). Mapowanie arch i `classify()` to polityka tierów mediaforge (→ zostaje w aplikacji). Nie wciągaj `classify` do pakietu.
- Checki odporne (False/pusty dict, nigdy wyjątek). Klucze dostawców jako booleany — **nigdy nie zwracaj wartości sekretów**.
