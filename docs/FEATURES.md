# Backlog funkcji

Pomysły poza ścieżką główną (ROADMAP). Przesiane z czterech analiz (Grok, Gemini v1/v2, eksport ChatGPT) + własne. Sortowanie orientacyjne wg wartości/kosztu.

## Wysoka wartość, rozsądny koszt
- **Profile źródeł (per domena, definiowane przez użytkownika)** — silnik, auth, jakość, kategoria/tagi, nazwa, tryb notatki, język; dopasowanie po domenie (S2/S5). Bez wbudowanego katalogu platform.
- **Rejestr dostawców chmury per zadanie** — Claude/ChatGPT/Gemini/DeepSeek przez LiteLLM, wybór modelu osobno dla nazwy/streszczenia/VLM/długiego kontekstu/RAG (S4).
- **Profile obliczeniowe per maszyna (tiery A/B/C)** — wg architektury GPU i VRAM; próg local→cloud + ręczne nadpisanie (S3). Obsługuje słabszy sprzęt (np. 1070: transkrypcja lokalnie, LLM/VLM chmura).
- **Przełącznik „tylko lokalnie"** per kategoria/materiał — blokuje wysyłkę transkryptu do chmury (S4).
- **Tryby notatek per dziedzina** — medyczny / techniczny / programistyczny / biznesowy (S4, częściowo).
- **OCR/VLM slajdów zsynchronizowany z mową** — „tekst slajdu + komentarz prowadzącego" (S6).
- **Klikalne timestampy** transkryptu ↔ odtwarzanie (S3).
- **Raport zbiorczy z konferencji** — lista sesji, wnioski, linki do momentów (S8).
- **Eksport do Obsidian/Notion/Logseq** — notatka + linki + rozdziały + pojęcia (S8).
- **Fiszki Anki/Quizlet** z transkryptu i streszczenia (S8).
- **Wykrywanie duplikatów** — perceptual hash / porównanie metadanych + transkryptu.
- **Auto-update yt-dlp i modeli** (S8).
- **Raport biblioteki** — ile GB / godzin / plików.
- **System tray + powiadomienia systemowe** o zakończeniu zadania.
- **Nasłuch schowka** na linki (z potwierdzeniem, nie auto-akcja).

## Średnia wartość
- **Tłumaczenie transkryptu** (przez LiteLLM) — przydatne przy konferencjach międzynarodowych.
- **Burn-in napisów** do pliku wideo.
- **Podstawowy edytor** — przycinanie, wycinanie przerw, łączenie (FFmpeg).
- **Wykrywanie definicji/procedur/wzorów** jako osobne sekcje notatki.
- **Lista „do powtórki"** generowana z materiału.
- **Notatki/tagi do segmentów** („ważne na egzamin", „do wdrożenia").
- **Integracja z QNAP NAS** jako katalog docelowy biblioteki.
- **Wiele języków interfejsu** (PL + EN).

## Niższy priorytet / większy koszt lub ryzyko
- **Pytania do całej biblioteki (RAG)** — częściowo S7; rozbudowa jako asystent wiedzy.
- **Nagrywanie live streamów** z dzieleniem na części.
- **Harmonogram zadań** (np. pobieranie playlist kursów w nocy) — tylko źródła bez DRM.
- **Rozszerzenie do przeglądarki** — duży koszt utrzymania, łatwo wejść w szarą strefę; rozważyć ostrożnie.

## Naturalni kandydaci na pluginy (po 1.0)

Funkcje, które najpierw powstają jako rejestry `Protocol` + extras, a kiedyś *mogą* stać się pluginami zewnętrznymi (entry points) — jeśli zbierze się ≥3 takie i będzie realna potrzeba osobnej instalacji:

- **Eksportery:** Obsidian / Notion / Logseq, Anki / Quizlet, burn-in napisów.
- **Tryby notatek dziedzinowych:** medyczny, techniczny, programistyczny, biznesowy (i kolejne).
- **Kanały powiadomień:** Telegram, powiadomienia systemowe, e-mail.
- **VLM / tłumaczenie:** opisy slajdów, tłumaczenie transkryptu.
- **Cele zapisu:** integracja z QNAP NAS / inne backendy storage.

Do czasu osiągnięcia progu: rejestr wewnętrzny + extra w `pyproject`, bez systemu pluginów.

## Świadomie odrzucone
- Obchodzenie DRM/TPM, dekrypcja strumieni, zrzut kluczy — patrz `LEGAL_BOUNDARIES.md`.
- Headless scraping „omijający 2FA" (Playwright Persistent Context w trybie bota) — patrz `CLAUDE.md`.
- Celery + Redis, FastAPI, Tauri/Electron — przerost dla aplikacji jednoużytkownikowej.
- Lokalny model 70B / pełny long-context na 24 GB VRAM — błąd VRAM vs RAM; długie konteksty → chmura przez LiteLLM.
- Wbudowana przeglądarka do automatycznego logowania — tylko ręczne logowanie/cookies-from-browser.
