# mediaforge

> **Nazwa robocza** — `mediaforge` jest placeholderem w konwencji `*forge` (jak `epubforge`, `icoforge`). Alternatywy do rozważenia: `lectorforge`, `scribeforge`, `mediascribe`. Zmień przed pierwszym publicznym commitem (`git grep -l mediaforge`).

Desktopowa aplikacja (Windows) do **legalnej archiwizacji materiałów edukacyjnych, szkoleniowych i konferencyjnych** oraz zamiany ich w przeszukiwalne, prywatne archiwum wiedzy z AI: nagrywanie ekranu/audio, import lokalnych plików, transkrypcja, streszczenia i notatki edukacyjne, rozdziały, tagi, OCR/VLM slajdów oraz wyszukiwanie po całej bibliotece.

Rdzeniem produktu **nie jest downloader**, tylko biblioteka wiedzy. Pobieranie bezpośrednie (yt-dlp) to funkcja dodatkowa, świadomie zaplanowana na późny etap.

## Status

🚧 Faza scaffoldu / pre-alpha. Patrz [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Zakres i ograniczenia

- **Obsługiwane treści:** materiały bez DRM, do których masz legalny dostęp (m.in. YouTube, Vimeo, strony uczelni i konferencji, webinary i materiały szkoleniowe bez technicznych zabezpieczeń), oraz pliki, które posiadasz lokalnie.
- **DRM:** treści zabezpieczone (Widevine/PlayReady) **nie są obsługiwane** — aplikacja nie obchodzi technicznych środków zabezpieczających i nie jest do tego przeznaczona. Docelowe podejście do tego obszaru — do ustalenia na późniejszym etapie, wyłącznie w granicach prawa. Patrz [`docs/LEGAL_BOUNDARIES.md`](docs/LEGAL_BOUNDARIES.md).
- **Logowanie:** wyłącznie poprzez `--cookies-from-browser` lub ręczne logowanie użytkownika. **Bez** automatyzacji obchodzącej zabezpieczenia (żadnego headless scrapingu „omijającego 2FA").

## Stos technologiczny

| Warstwa | Wybór |
|---|---|
| Język / GUI | Python 3.12, PySide6 (LGPL — zgodne z MIT) |
| Wspólne GUI | **chodzkos-gui-kit** (tag `v0.5.0`, extras `[qt]`) — paleta marki, `ThemeManager`, titlebar DWM, dialogi, widgety `PathEntry`/`FileList`/`LogView`/`HelpWindow` |
| Standard GUI | [`GUI_STANDARD.md`](https://github.com/chodzkos/gui-kit/blob/main/GUI_STANDARD.md) — mieszka w repo kitu, wersjonowany z nim (akcent `#5DCAA5`) |
| Pobieranie | yt-dlp (etap późny) |
| Media / nagrywanie | FFmpeg (binarka, subprocess), NVENC (HEVC/AV1), WASAPI loopback |
| Transkrypcja | backend wymienny: **whisper.cpp (default)** / faster-whisper (float16) / insanely-fast-whisper (tor mocy) |
| Diaryzacja (opcja) | pyannote.audio |
| Slajdy (opcja) | detekcja zmian klatek + VLM (lokalnie lub chmura) |
| Streszczenia | przez istniejący gateway **Ollama + LiteLLM** (lokal + fallback chmura) |
| Biblioteka | SQLite + FTS5 → później baza wektorowa (semantic search/RAG) |
| Kolejka zadań | pula QThread + tabela `jobs` w SQLite (bez Celery/Redis) |
| Sekrety | `keyring` (systemowy magazyn poświadczeń) |
| Powiadomienia | Telegram Bot API (opcjonalnie) |

## Architektura w skrócie

Pojedynczy pakiet z trzema warstwami: rdzeń niezależny od UI (`core/`), GUI (`gui/`), CLI (`cli/`). Akwizycja oparta na wzorcu strategii (`AcquisitionEngine`) — analogicznie do wielosilnikowego `pdf2md`. Pełny opis: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Instalacja (dev)

```bash
uv sync                    # pociąga też chodzkos-gui-kit z gita (pin do taga v0.5.0)
uv run mediaforge          # GUI
uv run mediaforge-cli --help
```

Wymagania zewnętrzne (poza PyPI): **FFmpeg** w PATH; opcjonalnie build **whisper.cpp** z CUDA; sterowniki NVIDIA dla NVENC. Podniesienie wersji gui-kit = osobny commit `chore:` + przebieg testów (pin do taga, nigdy `main`).

## Konwencje projektu

uv · ruff · mypy · pytest + pytest-qt · Conventional Commits · MIT. Brief dla Claude Code i pułapki: [`docs/CLAUDE.md`](docs/CLAUDE.md).

## Licencja

MIT — patrz [`LICENSE`](LICENSE). FFmpeg i whisper.cpp są wywoływane jako osobne binarki (subprocess), nie linkowane statycznie — kod aplikacji pozostaje MIT.
