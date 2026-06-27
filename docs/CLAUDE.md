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
- **FFmpeg jako subprocess**, nie biblioteka linkowana — utrzymuje licencję MIT i izoluje crash. NVENC do enkodowania (HEVC/AV1), WASAPI loopback do dźwięku systemowego.
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
