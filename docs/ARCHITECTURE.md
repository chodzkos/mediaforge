# Architektura

## Zasada naczelna

Rdzeń niezależny od UI. Pojedynczy pakiet, trzy warstwy:

```
src/mediaforge/
├── core/            # logika domenowa, zero importów Qt
│   ├── engines/     # akwizycja: wzorzec strategii (jak silniki w pdf2md)
│   │   ├── base.py          # Protocol: AcquisitionEngine
│   │   ├── recorder.py      # nagrywanie ekranu/audio (FFmpeg + WASAPI)
│   │   ├── importer.py      # import lokalnego pliku
│   │   └── downloader.py    # yt-dlp (etap S5, bez DRM)
│   ├── ai/
│   │   ├── transcribe.py    # Protocol: TranscriptionBackend (whisper.cpp/ct2/hf)
│   │   ├── diarize.py       # opcjonalna diaryzacja (pyannote)
│   │   ├── summarize.py     # klient LiteLLM gateway (lokal + chmura)
│   │   └── slides.py        # detekcja zmian klatek + opis VLM
│   ├── library/             # SQLite: model danych, FTS, później wektory
│   ├── jobs/                # kolejka: ThreadPoolExecutor (std-lib) + tabela jobs (Qt-free; bez Celery/Redis)
│   ├── config.py            # cienka warstwa nad Config z gui-kit (platformdirs + atomowy zapis)
│   ├── secrets.py           # keyring (sekrety — poza zakresem gui-kit)
│   ├── dependencies.py      # doctor: detekcja narzędzi/GPU (nvidia-smi, bez torcha) → zasila compute + status bar
│   └── naming.py            # szablony nazw + nazwa z AI
├── gui/             # PySide6 — wpina chodzkos-gui-kit (ThemeManager, dialogi, widgety)
└── cli/             # Typer (te same operacje co GUI, headless)
```

**Dlaczego desktop, nie web-serwis:** to aplikacja jednoużytkownikowa, potrzebuje natywnego dostępu do ekranu, dźwięku systemowego (WASAPI loopback) i GPU. Żadnego FastAPI/Celery/Redis ani Tauri/Electron — zostajemy w torze PySide6, który już znasz z `pdf2md`/`icoforge`/`epubforge`.

## Wzorzec silników akwizycji

Jeden Protocol, wiele implementacji — auto-dobór z możliwością ręcznego nadpisania:

```python
class AcquisitionEngine(Protocol):
    name: str
    def can_handle(self, source: Source) -> bool: ...
    def probe(self, source: Source) -> list[QualityOption]: ...
    def acquire(self, source: Source, opts: AcquireOptions,
                progress: ProgressCb) -> MediaArtifact: ...
```

Implementacje: `RecorderEngine` (rdzeń produktu), `ImporterEngine`, `DownloaderEngine` (yt-dlp, etap późny). Selektor: jeśli źródło to plik lokalny → importer; jeśli URL bez DRM obsługiwany przez yt-dlp → downloader; w pozostałych przypadkach → nagrywanie ekranu jako uniwersalny fallback.

## Potok danych (record-first)

```
Źródło (nagranie ekranu / import pliku / [S5] pobranie bez DRM)
  → surowy MediaArtifact (wideo/audio) zapisany do folderu materiału
  → ekstrakcja audio (FFmpeg)
  → [opcja] detekcja slajdów + opis VLM
  → transkrypcja (backend wymienny) [+ opcjonalna diaryzacja]
  → streszczenie / notatka edukacyjna (LiteLLM)
  → propozycja nazwy pliku z AI
  → zapis do biblioteki (SQLite) + powiadomienie (opcjonalnie Telegram)
```

Każdy krok to osobne zadanie w kolejce (`jobs`), wznawialne po awarii. Kolejka to **`ThreadPoolExecutor` w `core`** (Qt-free → działa też w headless CLI; QThread wymaga pętli Qt i złamałby regułę „core bez Qt"). Workery piszą tylko stan do tabeli `jobs` i **nie dotykają obiektów Qt**; GUI to adapter odświeżający widok przez `QTimer` (poll). Pipeline jest **sekwencyjny pod kątem VRAM** — Whisper, VLM i LLM nie ładują się równocześnie na 24 GB; każdy etap zwalnia GPU przed następnym.

## Układ „jeden materiał = jeden folder"

```
<biblioteka>/<kategoria>/<YYYY-MM-DD_tytuł>/
├── wideo.mp4
├── audio.m4a
├── transcript.srt          # + .vtt / .txt
├── transcript.json         # segmenty + (opcjonalnie) mówcy
├── slides.md               # opisy VLM + timestampy (opcja)
├── notatka.md              # streszczenie / notatka edukacyjna
└── metadata.json           # plik strukturalny (źródło prawdy obok SQLite)
```

SQLite jest indeksem/cache nad tymi folderami — folder pozostaje przenośny i samodzielny.

## Model danych (SQLite)

- `recordings` — id, title, source_type, source_url, category, created_at, duration, *_path, status, checksum, legal_note
- `jobs` — id, recording_id, job_type, status, progress, error_message, timestamps (kolejka + retry)
- `transcripts` — id, recording_id, language, model, text, segments_json
- `summaries` — id, recording_id, summary_type, model, content
- `tags`
- `source_profiles` — id, domain, engine, auth_method, quality_preset, category, tags, naming_template, note_mode, language (profile per domena, definiowane przez użytkownika; **dane relacyjne, odpytywane po domenie → SQLite**)
- Preferencje skalarne (motyw, ostatnie katalogi, profil obliczeniowy per maszyna, rejestr dostawców per zadanie) → **config.json przez `core/config.py`** (Config z kitu jest magazynem ustawień). `source_profiles` to jedyne dane configu trzymane w SQLite — bez osobnej tabeli `settings` (byłaby drugim magazynem ustawień obok config.json).
- FTS5 nad `transcripts.text` i `summaries.content` (S7); baza wektorowa dołożona później.

## Transkrypcja — backend wymienny (kluczowe pod Blackwell)

```python
class TranscriptionBackend(Protocol):
    def transcribe(self, audio: Path, opts: TranscribeOptions) -> Transcript: ...
```

- **`whispercpp` (DEFAULT)** — build CUDA whisper.cpp; bez PyTorcha/CTranslate2/transformers → omija cały problem sm_120.
- **`faster_whisper`** — działa na sm_120 **tylko** z `compute_type="float16"` (int8 → `CUBLAS_STATUS_NOT_SUPPORTED`).
- **`insanely_fast_whisper`** — tor mocy; wymaga nightly PyTorcha (cu12x) + pinów transformers + FA na Blackwell (patrz CLAUDE.md).
- **`cloud`** — auto-fallback na słabszym sprzęcie / dla bardzo długich materiałów, przez ten sam mechanizm co LiteLLM.

Auto-detekcja: przy starcie sprawdzamy dostępność CUDA i VRAM → wybór toru lokalnego lub chmury; zawsze z ręcznym nadpisaniem w ustawieniach.

## Streszczenia — przez istniejący gateway

Nie stawiamy własnego vLLM. `summarize.py` mówi do **Twojego LiteLLM** (lokalnie Devstral 24B / Qwen 14B, fallback chmura). Long-context: lokalnie realne ~30–40k tokenów (2–3h transkryptu na ~24B z kwantyzacją KV); dłuższe materiały → model chmurowy o dużym oknie, też przez LiteLLM. Hierarchiczne (sekcyjne) streszczanie tylko gdy materiał przekracza dostępne okno — nie jako domyślny prymitywny chunking.

## Profile obliczeniowe (per maszyna)

`core/compute.py` klasyfikuje maszynę w jeden z tierów — i o tym decyduje **VRAM oraz architektura GPU**, nie systemowy RAM:

- **Tier A** (nowoczesny GPU ≥16 GB, np. RTX 5090): transkrypcja, LLM i VLM lokalnie.
- **Tier B** (słabszy/starszy GPU, np. GTX 1070 8 GB / Pascal): transkrypcja lokalnie (mniejszy model Whisper — whisper.cpp radzi sobie i na Pascalu), LLM mały lokalny *możliwy* ale powolny → domyślnie chmura, VLM → chmura.
- **Tier C** (brak/za słaby GPU): wszystko w chmurze przez LiteLLM.

Profil jest zapisywany per maszyna (jak „per-machine suggestion" w pdf2md), zawsze z ręcznym nadpisaniem. Niuans Pascala vs Blackwella: stary 1070 ma tu *mniej* problemów niż 5090 — crash `int8` w CTranslate2 dotyczy wyłącznie sm_120, nie Pascala.

## Profile źródeł (per domena)

Definiowane przez użytkownika, dopasowywane po domenie wpisanego URL. Profil przykrywa domyślne `AcquireOptions` + opcje AI: silnik (yt-dlp/nagrywanie), metoda logowania (które cookies / „zaloguj ręcznie"), preset jakości, kategoria i tagi, szablon nazwy, tryb notatki (np. medyczny dla platformy CME), język.

**Świadoma decyzja:** profile są lokalne i tworzone przez użytkownika — repo **nie** zawiera wbudowanego katalogu „jak ripować nazwane platformy" (źle wygląda, ociera się o ToS, i tak gnije). Profil nigdy nie zawiera przepisów na obejście zabezpieczeń (patrz `LEGAL_BOUNDARIES.md`).

## Dostawcy chmury (rejestr per zadanie)

`core/ai/providers.py`. Routing realizuje istniejący gateway **LiteLLM** (jedno OpenAI-kompatybilne API → Anthropic/OpenAI/Google/DeepSeek), więc wybór dostawcy jest niemal darmowy — wystarczy lista modeli + klucze per dostawca w keyring.

Kluczowe: **wybór modelu per zadanie**, bo dostawcy nie są wymienni jeden do jednego:

| Zadanie | Uwaga |
|---|---|
| Nazwa pliku | tani model wystarczy |
| Streszczenie / notatka | mocniejszy model dla dokładnych notatek (np. medycznych) |
| Długi transkrypt | duże okno kontekstowe (Gemini/Claude) |
| Opis slajdów (VLM) | **wymaga vision** (Gemini/Claude/GPT; DeepSeek słabiej) |
| RAG / pytania do biblioteki | model do syntezy |

`ProviderRegistry.validate()` ostrzega o niedopasowaniu (np. model bez vision przypisany do slajdów). Dodatkowo przełącznik **„tylko lokalnie"** per kategoria/materiał blokuje wysyłkę do chmury, gdy nie chcesz, by transkrypt opuszczał maszynę.

## Punkty rozszerzeń (rejestry → pluginy)

Modularność budujemy dwustopniowo, żeby nie wpaść w over-engineering:

**Stopień 1 — rejestry wewnętrzne (od początku).** Każda rozszerzalna kategoria to `Protocol` + rejestr, dokładnie jak `AcquisitionEngine` i `TranscriptionBackend`. Naturalne kategorie: eksportery (Obsidian/Anki/Quizlet), tryby notatek (medyczny/techniczny/…), style streszczeń, kanały powiadomień (Telegram), backendy transkrypcji, VLM/tłumaczenie. Opcjonalne ciężkie zależności (torch, pyannote, baza wektorowa) izolujemy przez **extras w `pyproject`** + profil obliczeniowy — to realizuje „instaluj tylko co potrzebne" bez osobnego systemu.

**Stopień 2 — pluginy zewnętrzne (kandydat po 1.0).** Dopiero gdy ≥3 funkcje realnie tego chcą: discovery przez **entry points** (`importlib.metadata`, grupa `mediaforge.<kategoria>`), plugin = osobna pip-instalowalna paczka. Bez własnego loadera. Konwencja nazewnictwa grup jest wspólna dla projektów (pdf2md, mediaforge), a wspólny kod ekstrahujemy do dzielonej paczki po sprawdzeniu w ≥2 projektach — ta sama reguła co przy gui-kit.

## Integracja z chodzkos-gui-kit

Wszystkie aplikacje (chodzkos) stoją na wspólnym kicie. mediaforge **konsumuje** kit, nie kopiuje standardu — `GUI_STANDARD.md` mieszka w repo kitu i wersjonuje się z nim; my pinujemy tag (`v0.5.0`, extras `[qt]`) i podążamy za nim.

**Co bierzemy z kitu (nie piszemy od zera):**

| Potrzeba mediaforge | Komponent kitu |
|---|---|
| Motyw + auto dark/light + titlebar DWM | `qt.theme.ThemeManager` (`apply`, `attach_titlebar`) |
| Magazyn konfiguracji | `config.Config` (platformdirs + atomowy zapis + flaga dirty) — owinięty w `core/config.py` |
| Dialogi plików (reguła rozjazdu, fallback) | `qt.dialogs.open_file/open_files/save_file/pick_dir` |
| Pole katalogu wyjściowego | `qt.widgets.PathEntry` |
| Lista importu / biblioteki (z D&D) | `qt.widgets.FileList` |
| Strumień logu nagrywania/transkrypcji | `qt.widgets.LogView` — parametr **`level_colors`** (zaprojektowany pod streaming, np. `{"transcribing": ...}`) |
| Okno pomocy / dokumentacja w aplikacji | `qt.widgets.HelpWindow` (zakładki `(tytuł, html)`) + helpery `help_html` (`section`/`paragraph`/`table`/`code`/`preformatted` — kolory przez `palette(...)`, zero hexów) |
| Ikony przebarwialne wg motywu | `qt.icons.get_icon` / `ICON_MAP` / `clear_cache` |

**Reguły konsumenta (egzekwowane też w kicie):**
- **Pin do TAGA**, nigdy `main`. Podniesienie wersji = osobny commit `chore:` + testy.
- **Zero hardcodowanych hexów** w `gui/` — kolory wyłącznie z palety/ról kitu (hexy żyją tylko w `palette.py` kitu).
- **Nie nadstylizowuj generycznych widgetów** (`QToolButton`, `QLineEdit`, `QComboBox`) globalnym QSS — app-QSS przecieka do nienatywnych dialogów kitu i psuje przypięte przyciski (pułapka §4 standardu). Stylizuj per-widget.
- **Debounce configu** po stronie GUI: `Config(on_dirty=...)` → `QTimer` → `flush()` po ~1 s.

**Luka do uzupełnienia (ikony):** obecny zestaw ikon kitu pochodzi z IcoForge/EpubForge (edytor, pliki: pencil/eraser/save/folder-open…). Brakuje ikon specyficznych dla mediaforge: nagrywanie, mikrofon, pobieranie, napisy/transkrypt, streszczenie. Zgodnie z regułą kitu „kod wchodzi przez ekstrakcję ze sprawdzonej aplikacji" — najpierw używamy ikon w mediaforge (lokalnie), a po sprawdzeniu dokładamy SVG (Lucide/ISC) do `assets/icons/` + `ICON_MAP` kitu jako osobny PR do gui-kit. Do tego czasu: `standardIcon` Qt lub lokalny fallback. **Reguła trzech** dla nowych wspólnych widgetów obowiązuje tak samo (ekstrakcja dopiero przy ≥2 konsumentach).

## Diagnostyka środowiska (doctor)

`core/dependencies.py` — wzorzec przeniesiony z pdf2md (funkcje odporne na brak narzędzia: zwracają False/pusty dict, nigdy nie rzucają; `check_all()` jako agregat). Qt-free. Sprawdza: system, ffmpeg (+ które enkodery: `h264_nvenc`/`hevc_nvenc`/`av1_nvenc`/`libx264`), whisper.cpp (binarka), GPU, gateway LiteLLM (endpoint z configu), klucze dostawców w keyring (**same booleany, nigdy wartości**).

**Detekcja GPU bez torcha** — kluczowa różnica względem pdf2md. Domyślne ścieżki mediaforge (whisper.cpp, NVENC) są torch-free, więc doctor używa `nvidia-smi`, a sonda torcha (`torch.zeros(1).cuda()`) to wyłącznie dodatek odpalany tylko, gdy torch jest zainstalowany (tor HF). Sonda nvidia-smi jest odporna jak reszta: gate `command_in_path` (brak sterowników/inny GPU → puste), sprawdzenie `returncode` (sterownik w złym stanie → puste), brak parsowania stderr. **compute_cap pobierane osobnym zapytaniem** (starsze sterowniki mogą nie mieć tego pola), a gdy nie przyjdzie lub zwróci `[Not Supported]` → **arch mapowany z nazwy GPU** (`arch_from_name`: RTX 5090→Blackwell, GTX 1070→Pascal). Dzięki temu `classify()` dostaje arch niezależnie od wersji nvidia-smi.

**Granica ekstrakcji GPU (ważne na później):** `check_gpu()` zwraca **surowe fakty** (nazwa/VRAM/compute_cap) i to jest część generyczna → do kitu. Mapowanie arch (`detect_arch`/`arch_from_name`/`resolved_arch`) oraz `classify()` to **mediaforge-specyficzna polityka tierów A/B/C** → zostaje w aplikacji. Przy ekstrakcji: kit daje `check_gpu()` (surowe), mediaforge bierze te dane, mapuje arch i woła swój `classify()`. **Nie wciągać `classify` do kitu** — to polityka, nie generyczna sonda. (`check_all()` wzbogaca sekcję GPU o arch dla wyświetlania, ale samo `check_gpu` pozostaje surowe.)

**Domknięcie `compute.py`** — `detect_arch(compute_cap)` mapuje compute capability na `GPUArch` (sm_120→Blackwell, 6.1→Pascal, …) i karmi `compute.classify()`, które dotąd dostawało `arch=UNKNOWN`. Dzięki temu doctor raportuje rozwiązany **tier A/B/C** i co leci lokalnie vs chmura. To samo `check_all()` zasila status bar GUI — **jedno źródło detekcji**, GUI nie powtarza wykrywania.

Doctor rodzi się jako osobny przyrost po S0 i rośnie z zależnościami: S1 dokłada test enkoderów NVENC, S3 sprawdzenie buildu whisper.cpp (+ opcjonalna sonda torcha), S4 modele LiteLLM i dostawców. Komenda CLI: `mediaforge-cli doctor` (render przez `render_report`, opcja `--json` dla surowych danych).

### Warstwy i ścieżka do kitu

Doctor to nie jeden byt, tylko trzy warstwy — i tylko część jest współdzielna:

- **Warstwa 1 — prezentacja** (`render_report`): generyczny render sekcja/status/hint, operuje wyłącznie na danych z `check_all()`. App-niezależna → docelowo do kitu.
- **Warstwa 2 — sondy uniwersalne** (`command_in_path`, `api_key_present`, `check_gpu`): generyczne → docelowo do kitu.
- **Warstwa 3 — definicje „co sprawdzać"** (ffmpeg, whisper.cpp, silniki transkrypcji): wyłącznie mediaforge → **zostaje w aplikacji**.

Kluczowe: mediaforge buduje doctora **z rozdzieloną prezentacją od sond** (dane w `check_all()`, render osobno) — inaczej niż pdf2md, którego `doctor()` splata budowanie tabel z sondami inline. Dzięki temu **mediaforge jest czystym wzorcem do ekstrakcji**, a nie pdf2md.

Ekstrakcja **z działającego kodu, nie z przewidywań**: nie projektujemy frameworka teraz. Gdy mediaforge doctor działa, mamy dwa działające (pdf2md + mediaforge) + trzecią potrzebę (EpubForge: Java/Calibre/Pandoc) → reguła trzech spełniona. Wtedy ekstrahujemy **cienki** framework (warstwa 1 + prymitywy warstwy 2) ze wzorca mediaforge; **pdf2md migruje** na niego (rozplątując swoje inline-tabele), a EpubForge konsumuje go jako trzeci (własne sondy), **nie jako trzecią kopię**. Framework jest cienki, bo sondy mediaforge są **heterogeniczne** (ffmpeg vs GPU vs LiteLLM — różne kształty), więc współdzieli się mechanizm, nie bogata struktura silnikowa.
