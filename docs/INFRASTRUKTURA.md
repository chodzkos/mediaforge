# Infrastruktura mediaforge — instalacja i uruchamianie

Doctor (`uv run mediaforge-cli doctor`) pokazuje stan wszystkich zależności. Ten dokument
mówi, jak je postawić i jak naprawiać typowe problemy.

## FFmpeg
- Nowoczesne GPU NVIDIA (RTX 20xx+, sterownik ≥610): FFmpeg 8.x/git z gyan.dev — OK.
- **GTX 10xx (Pascal): FFmpeg 7.x RELEASE** (nie 8.x/git!). FFmpeg 8.x wymaga sterownika
  ≥610, który dla Pascala NIE ISTNIEJE — NVENC będzie martwy, nagrania spadną na CPU
  (szarpanie). Zmierzone na GTX 1070: 7.1 release = NVENC działa.
- AMD (Radeon/APU, np. 780M): enkoder h264_amf — doctor pokaże ✓ tylko na sprzęcie AMD.

## Ollama (modele lokalne)
- Instalacja WINDOWSOWA (ollama.com) — nie w WSL!
- **Pułapka WSL:** linuksowy instalator zakłada usługę systemd, która wstaje z dystrybucją
  i okupuje port 11434 przez wslrelay (Windows widzi ją zamiast natywnej; stare wersje,
  modele czytane przez wolny most /mnt/c). Objaw: `ollama -v` pokazuje różne wersje
  klient/serwer. Naprawa: `wsl -e bash -lc "sudo systemctl disable --now ollama"` +
  `wsl --shutdown`, potem start windowsowej Ollamy.
- Modele: `ollama pull qwen3.6:27b` (streszczenia), `ollama pull qwen3:14b` (szybszy),
  `ollama pull qwen3-vl:8b` (analiza slajdów). Żyją w %USERPROFILE%\.ollama — reinstalacja
  Ollamy ich nie rusza.
- Weryfikacja obciążenia: `ollama ps` W TRAKCIE generowania (PROCESSOR ma być 100% GPU;
  kolumna CONTEXT pokazuje realny num_ctx).

## LiteLLM (gateway — jedyna droga aplikacji do modeli)
- Instalacja/aktualizacja — ZAWSZE z Pillow (wymagany do obrazów/VLM; bez niego analiza
  slajdów pada błędem "No module named 'PIL'"):
      uv tool uninstall litellm            # przy reinstalacji/naprawie
      uv tool install "litellm[proxy]" --with Pillow
      litellm --version                    # ZAWSZE zweryfikuj launcher po instalacji
- Config: C:\litellm\litellm_config.yaml (poza repo — może kiedyś zawierać sekrety):
      model_list:
        - model_name: qwen-local
          litellm_params: { model: ollama/qwen3.6:27b, api_base: http://localhost:11434, num_ctx: 32768 }
        - model_name: qwen-local-fast
          litellm_params: { model: ollama/qwen3:14b, api_base: http://localhost:11434, num_ctx: 32768 }
        - model_name: qwen-vl-local
          litellm_params: { model: ollama/qwen3-vl:8b, api_base: http://localhost:11434 }
      litellm_settings:
        drop_params: true
- Start (osobny terminal, musi chodzić podczas streszczeń/notatek; nagrywanie i
  transkrypcja działają BEZ gatewaya):
      litellm --config C:\litellm\litellm_config.yaml --port 4000
- Weryfikacja: curl.exe -s http://localhost:4000/v1/models  → lista trzech modeli.
- Po KAŻDEJ edycji yaml: restart gatewaya (config czyta się przy starcie).

## whisper.cpp (transkrypcja)
- Binarka: wskazywana w configu kluczem `whispercpp_path` (binarka bywa self-compiled poza
  PATH, np. `…\whisper.cpp\build\bin\whisper-cli.exe`); brak wartości → autodetekcja przez
  PATH (`shutil.which whisper-cli`). Sprawdź `doctor` (linia whisper.cpp: ✓/✗ + ścieżka).
- Model: ścieżka do pliku `.bin` w configu kluczem `whisper_model` (ggml, np.
  `ggml-large-v3.bin` — dokładniejszy, wolniejszy; `ggml-medium.bin` — kompromis). Brak
  wartości → transkrypcja niedostępna (doctor: „model nieustawiony").
- Build CUDA (przepis upstream whisper.cpp — self-compile, binarka w `build/bin/`):
      cmake -B build -DGGML_CUDA=1
      cmake --build build --config Release
  Pascal (GTX 10xx, sm_61) działa z torem ggml/CUDA (whisper.cpp schodzi niżej niż tory
  torchowe). Ustaw `whispercpp_path` na powstałą binarkę.
- Doctor pokazuje "runtime: CUDA" gdy działa na GPU.

## Audio systemowe (nagrywanie z dźwiękiem)
- VB-Cable (vb-audio.com): CABLE Input = domyślne urządzenie ODTWARZANIA; na CABLE Output
  włączyć "Nasłuchuj tego urządzenia" → głośniki (żeby słyszeć to, co się nagrywa).
- Format po obu stronach kabla: 16 bit 48000 Hz (Właściwości → Zaawansowane).

## Pobieranie (yt-dlp)
- Treści za logowaniem: WYŁĄCZNIE Firefox ("Użyj sesji przeglądarki: firefox"; zaloguj się
  w Firefoksie i ZAMKNIJ go przed pobraniem). Chrome/Edge/Opera na Windows szyfrują cookies
  (App-Bound Encryption) w sposób nieodczytywalny — nie zadziałają.

## Konfiguracja aplikacji
- Zmiany configu przez obiekt (nigdy ręczna edycja JSON):
      uv run python -c "from mediaforge.core.config import load; c=load(); c.set('KLUCZ', WARTOSC); c.flush()"
- Windows i WSL mają OSOBNE configi (platformdirs) — ustawienia nie przenoszą się między nimi.
- Po `git pull`/merge: ZAMKNIJ i uruchom GUI ponownie (proces trzyma kod z chwili startu —
  "naprawione na dysku" ≠ "naprawione w działającym oknie").

### Sufiksy system-promptu (`/no_think`) — klucze dla zaawansowanych
- **`summary_prompt_suffix`** (streszczenia/notatki-LLM) i **`vlm_prompt_suffix`** (analiza slajdów
  VLM) to OSOBNE klucze. Oba domyślnie `/no_think` — soft-switch qwen3, wyłącza tryb rozumowania,
  by cały budżet tokenów szedł w treść (bez tego model rozumujący zjada limit na rozumowanie i
  zwraca pustą treść: „Model zużył cały limit tokenów na rozumowanie").
- **Nie ma pól w dialogu Ustawień** (świadomie — klucze dla zaawansowanych). Ustawiasz je tylko
  przez obiekt configu (jak wyżej).
- **Kiedy czyścić (`c.set('KLUCZ', '')`)**: gdy dany tor jedzie modelem **nie-rozumującym**
  (np. streszczenia przez chmurowy model bez trybu myślenia) — wtedy `/no_think` jest zbędny lub
  niepożądany. Pusty string (`''`) = jawne wyłączenie sufiksu; usunięcie klucza (albo brak) =
  powrót do domyślnego `/no_think`.
- **Klucze są niezależne**: wyczyszczenie `summary_prompt_suffix` (bo streszczasz modelem
  nie-rozumującym) **nie dotyka** `vlm_prompt_suffix` — VLM (qwen3-vl) dalej dostaje `/no_think`,
  którego KONIECZNIE potrzebuje. (Dawniej jeden wspólny klucz sterował oboma torami — patrz
  CHANGELOG, zmiana zachowania.)

## Szybka diagnoza
1. `uv run mediaforge-cli doctor` — stan wszystkiego; hinty przy ✗ pokazują przyczynę.
2. Streszczenie/notatka wisi lub pada → czy gateway chodzi? (terminal LiteLLM, /v1/models)
3. Model wolny → `ollama ps` w trakcie: PROCESSOR 100% GPU? CONTEXT rozsądny?
4. Nagranie szarpie → LogView: jaki enkoder? (GPU vs CPU); statystyki dup/drop w folderze
   materiału.
