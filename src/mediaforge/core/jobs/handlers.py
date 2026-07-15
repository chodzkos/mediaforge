"""Handlery zadań kolejki (Qt-free) — transkrypcja i import.

Łączą kolejkę (:mod:`core.jobs`) z silnikami (transkrypcja whisper.cpp, import A/V) i
biblioteką (``metadata.json`` = źródło prawdy + indeks SQLite). Każdy handler to domknięcie
``(job, progress) -> None`` rejestrowane w :class:`JobQueue`. Wyjątek handlera → kolejka
oznacza job jako błędny (status + komunikat). Bez Qt — GUI tylko odpytuje statusy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from mediaforge.core.ai.chunking import Chunk, split_segments
from mediaforge.core.ai.pairing import SlideWindow, pair_slides_with_segments
from mediaforge.core.ai.routing import ModelRoute, assert_route_allowed, resolve_route
from mediaforge.core.ai.summarize import (
    SAFE_CONTEXT_TOKENS,
    TRUNCATED_MARK,
    GatewayError,
    SummaryClient,
    SummaryResult,
    estimate_tokens,
    fit_reduce_budget,
    map_prompt,
    part_section,
    read_transcript_segments,
    reduce_parts,
    summary_start_line,
)
from mediaforge.core.ai.transcribe import Segment, TranscribeOptions, TranscriptionBackend
from mediaforge.core.ai.vision import SlideAnalysis, VisionClient
from mediaforge.core.config import DEFAULT_SUMMARY_CHUNK_CHARS, DEFAULT_SUMMARY_REDUCE_MAX_TOKENS
from mediaforge.core.engines.download_engine import DownloaderEngine
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs.store import Job, JobStore
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.core.library.slides import SLIDES_DIRNAME, Slide

logger = logging.getLogger("mediaforge")

SUMMARY_FILENAME = "summary.md"
SUMMARY_PARTS_FILENAME = "summary_parts.md"
NOTES_FILENAME = "notes.md"
# Analizy slajdów (VLM) zapisywane przyrostowo — plik maszynowy (JSON, BEZ BOM), wznowienie fazy 1.
SLIDES_ANALYSIS_FILENAME = "slides_analysis.json"

# Typy zadań (job_type) używane przez kolejkę.
JOB_TRANSCRIBE = "transcribe"
JOB_IMPORT = "import"
JOB_SUMMARIZE = "summarize"
JOB_NOTES = "notes"
JOB_DOWNLOAD = "download"

# Linie wykonawcze. GPU = jeden executor (max_workers=1) dzielony przez WSZYSTKIE zadania
# modelowe (transkrypcja, streszczenie modelem LOKALNYM, później VLM) → tylko jeden model w
# VRAM naraz (sequential VRAM z CLAUDE.md). IO = import (kopia+ffmpeg) oraz streszczenie
# modelem CHMUROWYM (żądanie sieciowe, nie obciąża GPU) — niezależne od GPU.
GPU_LANE = "gpu"
IO_LANE = "io"
# Domyślny rozmiar linii i trasy job_type→linia. JOB_SUMMARIZE domyślnie na GPU (wariant
# lokalny); wariant chmurowy nadpisuje linię na IO w momencie enqueue (payload['lane']).
DEFAULT_LANES: dict[str, int] = {GPU_LANE: 1, IO_LANE: 2}
DEFAULT_ROUTES: dict[str, str] = {
    JOB_TRANSCRIBE: GPU_LANE,
    JOB_IMPORT: IO_LANE,
    JOB_SUMMARIZE: GPU_LANE,
    JOB_NOTES: GPU_LANE,  # VLM+LLM lokalnie → GPU; obie trasy chmurowe nadpisują na IO (enqueue)
    JOB_DOWNLOAD: IO_LANE,  # sieć, nie GPU
}

_ProgressCb = Callable[[float], None]
_Handler = Callable[[Job, _ProgressCb], None]


def make_transcribe_handler(
    store: RecordingStore,
    backend: TranscriptionBackend,
    *,
    options: TranscribeOptions | None = None,
) -> _Handler:
    """Handler transkrypcji: materiał z biblioteki → whisper.cpp → metadata.json + SQLite.

    Zapis WYŁĄCZNIE przez ``replace`` na istniejących metadanych — pozostałe pola (tytuł,
    tagi, statusy) zachowane, ``transcript_status`` nie jest zerowany innym materiałom.
    Pliki .json/.srt lądują w folderze materiału (źródło prawdy).
    """
    opts = options or TranscribeOptions()

    def handler(job: Job, progress: _ProgressCb) -> None:
        if job.recording_id is None:
            raise ValueError("transcribe: brak recording_id w zadaniu")
        material = store.get_material(job.recording_id)
        if material is None:
            raise ValueError(f"transcribe: materiał {job.recording_id} nie istnieje")
        folder, meta = material
        relative = meta.audio_path or meta.video_path
        if not relative:
            raise ValueError("transcribe: materiał bez pliku audio/wideo")

        progress(0.1)
        # Procent z whisper-cli → jobs.progress (0..1). Throttle robi backend (zmiana %).
        result = backend.transcribe(
            folder / relative, folder, opts, on_progress=lambda pct: progress(pct / 100.0)
        )
        updated = replace(
            meta,
            transcript_status="done",
            transcript_json=result.json_path.name if result.json_path else None,
            transcript_srt=result.srt_path.name if result.srt_path else None,
        )
        write_metadata(folder, updated)  # metadata.json = źródło prawdy
        store.upsert_material(folder, updated)  # indeks SQLite
        progress(1.0)

    return handler


def summarize_lane(route: ModelRoute) -> str:
    """Linia dla streszczenia wg trasy: lokalna → GPU (jeden model w VRAM), chmurowa → IO.

    Decyzja podejmowana w momencie enqueue (na podstawie :func:`resolve_route`): streszczenie
    chmurowe to żądanie sieciowe, nie obciąża VRAM → idzie linią I/O i nie blokuje transkrypcji.
    """
    return IO_LANE if route.is_cloud else GPU_LANE


def _write_human_md(path: Path, text: str) -> None:
    """Zapis pliku .md czytanego przez człowieka z BOM (utf-8-sig).

    BOM WYŁĄCZNIE dla plików otwieranych w zewnętrznych aplikacjach (summary.md /
    summary_parts.md w Calibre/czytnikach windowsowych, które bez BOM zgadują cp1250 → krzaki
    w polskich znakach). NIE dla metadata.json / transcript.json (BOM łamie parsery JSON,
    RFC 8259) ani .srt (pisze je whisper-cli).
    """
    path.write_text(text, encoding="utf-8-sig")


def make_summarize_handler(
    store: RecordingStore,
    client: SummaryClient,
    *,
    local_model: str | None,
    cloud_model: str | None,
    chunk_chars: int = DEFAULT_SUMMARY_CHUNK_CHARS,
    reduce_max_tokens: int = DEFAULT_SUMMARY_REDUCE_MAX_TOKENS,
) -> _Handler:
    """Handler streszczenia: materiał → resolve_route → assert → gateway → summary.md + statusy.

    Długość zdejmowana strukturalnie (map-reduce): transkrypt cięty na kawałki po granicach
    segmentów whispera (:func:`split_segments`, próg ``chunk_chars``). Jeden kawałek → ścieżka
    POJEDYNCZA (jeden request, bez reduce, bez ``summary_parts.md``) — pełna zgodność z dawnym
    zachowaniem krótkich materiałów. Wiele kawałków → MAP (streszczenie każdego, zapis częściowy
    do ``summary_parts.md`` po każdym) i REDUCE (sklejenie w jedno ``summary.md``, hierarchicznie
    gdy cząstkowe też przekraczają próg).

    Egzekucja granicy prywatności jest podwójna: :func:`resolve_route` wybiera trasę wg
    ``cloud_ok`` materiału, a :func:`assert_route_allowed` (ostatnia linia obrony tuż przed
    wysyłką) blokuje wrażliwy materiał na trasie chmurowej — trasa rozstrzygana RAZ na job, więc
    wszystkie wywołania map i reduce idą tą samą, dozwoloną trasą. ``replace`` na istniejących
    metadanych nie zeruje pozostałych pól.
    """

    def handler(job: Job, progress: _ProgressCb) -> None:
        if job.recording_id is None:
            raise ValueError("summarize: brak recording_id w zadaniu")
        material = store.get_material(job.recording_id)
        if material is None:
            raise ValueError(f"summarize: materiał {job.recording_id} nie istnieje")
        folder, meta = material
        if meta.transcript_status != "done" or not meta.transcript_json:
            raise ValueError("summarize: brak transkryptu (najpierw transkrypcja)")

        route = resolve_route(
            cloud_ok=meta.cloud_ok, local_model=local_model, cloud_model=cloud_model
        )
        assert_route_allowed(route, cloud_ok=meta.cloud_ok)  # ostatnia linia obrony

        progress(0.1)
        segments = read_transcript_segments(folder / meta.transcript_json)
        chunks = split_segments(segments, max_chars=chunk_chars)
        parts_file = folder / SUMMARY_PARTS_FILENAME

        if len(chunks) <= 1:
            _finish_single(store, client, route, folder, meta, chunks, parts_file, progress)
            return

        summary_parts = _run_map(client, route, folder, chunks, parts_file, progress)
        final = _run_reduce(client, route, chunk_chars, summary_parts, progress, reduce_max_tokens)
        text = f"{final.text}\n\n{TRUNCATED_MARK}" if final.truncated else final.text
        _write_human_md(folder / SUMMARY_FILENAME, text)
        _persist_summary(store, folder, meta, parts_name=SUMMARY_PARTS_FILENAME)
        progress(1.0)

    return handler


def _finish_single(
    store: RecordingStore,
    client: SummaryClient,
    route: ModelRoute,
    folder: Path,
    meta: MaterialMetadata,
    chunks: list[Chunk],
    parts_file: Path,
    progress: _ProgressCb,
) -> None:
    """Ścieżka pojedyncza (0/1 kawałek): jeden request, identyczna z dawnym zachowaniem.

    Sprzątamy ewentualny stary ``summary_parts.md`` (materiał, który wcześniej szedł chunked,
    a teraz mieści się w jednym kawałku) i zerujemy ``summary_parts_path`` w metadanych.
    """
    text = chunks[0].text if chunks else ""
    summary = client.summarize(text, route)  # GatewayError → job.error z base_url
    parts_file.unlink(missing_ok=True)
    _write_human_md(folder / SUMMARY_FILENAME, summary)
    _persist_summary(store, folder, meta, parts_name=None)
    progress(1.0)


def _run_map(
    client: SummaryClient,
    route: ModelRoute,
    folder: Path,
    chunks: list[Chunk],
    parts_file: Path,
    progress: _ProgressCb,
) -> list[Segment]:
    """Faza MAP: streszcz każdy kawałek, dopisz do ``summary_parts.md`` po każdym (praca częściowa).

    Zapis częściowy chroni pracę: błąd w kawałku ``k`` propaguje się (job error), ale sekcje
    ``1..k-1`` są już na dysku, a komunikat wskazuje plik. Zwraca streszczenia cząstkowe jako
    segmenty (tekst + zakres czasu kawałka) — wejście fazy REDUCE.
    """
    total = len(chunks)
    logger.info(
        summary_start_line(
            sum(len(c.text) for c in chunks), route.model, client.config.timeout, chunks=total
        )
    )
    sections: list[str] = []
    parts: list[Segment] = []
    for chunk in chunks:
        try:
            res = client.run(map_prompt(chunk.index, total, chunk), route)
        except GatewayError as exc:
            raise GatewayError(
                f"{exc} (streszczenia cząstkowe 1..{chunk.index - 1} zachowane w {parts_file.name})"
            ) from exc
        if res.truncated:
            logger.warning(
                "Streszczenie części %s/%s prawdopodobnie ucięte (limit summary_max_tokens=%s) — "
                "rozważ zwiększenie limitu.",
                chunk.index,
                total,
                client.config.max_tokens,
            )
        sections.append(part_section(chunk.index, total, chunk, res.text, truncated=res.truncated))
        _write_human_md(parts_file, "".join(sections))  # przepisujemy całość → jeden BOM na starcie
        parts.append(Segment(start=chunk.start, end=chunk.end, text=res.text))
        # MAP zajmuje pasmo [0.1, 0.7]; reszta [0.7, 1.0] zostaje na REDUCE.
        progress(0.1 + 0.6 * chunk.index / total)
    return parts


def _run_reduce(
    client: SummaryClient,
    route: ModelRoute,
    chunk_chars: int,
    parts: list[Segment],
    progress: _ProgressCb,
    reduce_max_tokens: int,
) -> SummaryResult:
    """Faza REDUCE: sklej streszczenia cząstkowe w jedno (hierarchicznie, gdy trzeba).

    Reduce ma OSOBNY budżet wyjścia (``reduce_max_tokens``, > mapowego) — pisze najdłużej, a jego
    prompt jest mały, więc w oknie zostaje zapas. Guard okna (:func:`fit_reduce_budget`) przycina
    ten budżet, gdy sam prompt (dużo streszczeń cząstkowych) urósłby z wyjściem ponad ``num_ctx`` —
    lepiej krótsze wyjście niż ciche wypadnięcie promptu za okno.

    Postęp w paśmie [0.7, 1.0]: monotoniczny, krok po każdym wywołaniu reduce (liczba wywołań
    zależy od głębokości hierarchii, nieznanej z góry — stąd asymptotyczne zbliżanie do 1.0,
    domknięte ``progress(1.0)`` przez wołającego).
    """
    reduce_done = 0

    def reduce_call(user_content: str) -> SummaryResult:
        nonlocal reduce_done
        budget = fit_reduce_budget(user_content, reduce_max_tokens)
        if budget < reduce_max_tokens:
            logger.warning(
                "Reduce: prompt duży (~%s tok.) — budżet wyjścia przycięty %s→%s, by zmieścić się "
                "w oknie (~%s tok.).",
                estimate_tokens(user_content),
                reduce_max_tokens,
                budget,
                SAFE_CONTEXT_TOKENS,
            )
        res = client.run(user_content, route, max_tokens=budget)
        reduce_done += 1
        progress(min(0.99, 0.7 + 0.29 * reduce_done / (reduce_done + 1)))
        return res

    final, _calls = reduce_parts(parts, chunk_chars=chunk_chars, call=reduce_call)
    if final.truncated:
        logger.warning(
            "Finalne streszczenie prawdopodobnie ucięte (limit summary_max_tokens=%s) — "
            "rozważ zwiększenie limitu.",
            client.config.max_tokens,
        )
    return final


def _persist_summary(
    store: RecordingStore,
    folder: Path,
    meta: MaterialMetadata,
    *,
    parts_name: str | None,
) -> None:
    """Zapis statusów streszczenia: metadata.json (źródło prawdy) + indeks SQLite (round-trip)."""
    updated = replace(
        meta,
        summary_status="done",
        summary_path=SUMMARY_FILENAME,
        summary_parts_path=parts_name,
    )
    write_metadata(folder, updated)
    store.upsert_material(folder, updated)


def enqueue_summarize(
    store: RecordingStore,
    jobs: JobStore,
    recording_id: int,
    *,
    local_model: str | None,
    cloud_model: str | None,
) -> int:
    """Kolejkuje streszczenie: odmawia bez transkryptu, dobiera linię wg trasy (resolve_route).

    Odmowa (``ValueError``) gdy materiał nie istnieje albo ``transcript_status != done`` —
    „najpierw transkrypcja". Linia wybierana TU (nie w handlerze) na podstawie zgody materiału
    i skonfigurowanych modeli: lokalna → GPU, chmurowa → IO (payload['lane']). Zwraca id joba.
    """
    material = store.get_material(recording_id)
    if material is None:
        raise ValueError(f"summarize: materiał {recording_id} nie istnieje")
    _folder, meta = material
    if meta.transcript_status != "done":
        raise ValueError("Najpierw transkrypcja — brak transkryptu do streszczenia.")
    route = resolve_route(cloud_ok=meta.cloud_ok, local_model=local_model, cloud_model=cloud_model)
    return jobs.enqueue(
        JOB_SUMMARIZE, recording_id=recording_id, payload={"lane": summarize_lane(route)}
    )


# ── Notatka per slajd (S6): przepływ DWUFAZOWY (VLM → LLM) ─────────────────────


def notes_lane(vlm_route: ModelRoute, llm_route: ModelRoute) -> str:
    """Linia notatki: GPU, gdy KTÓRAKOLWIEK faza (VLM/LLM) idzie lokalnie (model w VRAM).

    Notatka to dwie fazy modelowe (VLM: analiza slajdów, potem LLM: komentarz). Dopóki
    którakolwiek jest lokalna, job musi trzymać linię GPU (jeden model w VRAM naraz, sequential
    VRAM). Dopiero OBIE trasy chmurowe → IO (żądania sieciowe nie obciążają GPU, nie blokują
    transkrypcji). Decyzja przy enqueue (jak w streszczeniu).
    """
    return IO_LANE if (vlm_route.is_cloud and llm_route.is_cloud) else GPU_LANE


def make_notes_handler(
    store: RecordingStore,
    vision_client: VisionClient,
    summary_client: SummaryClient,
    *,
    vlm_local: str | None,
    vlm_cloud: str | None,
    llm_local: str | None,
    llm_cloud: str | None,
) -> _Handler:
    """Handler notatki per slajd (S6): DWUFAZOWY (VLM → LLM), zapis ``notes.md`` + statusy.

    Warunki wejścia (odmowa ``ValueError``): materiał ma slajdy (≥1) ORAZ ``transcript_status ==
    done``. FAZA 1 (VLM): analiza KAŻDEGO slajdu, przyrostowo do ``slides_analysis.json``
    (wznowienie po błędzie pomija już przeanalizowane). Wszystkie wywołania VLM POD RZĄD, dopiero
    potem FAZA 2 (LLM) — naprzemienne VLM/LLM co slajd zmuszałoby Ollamę do przeładowania modeli
    2N razy (thrash VRAM); fazami każdy model ładuje się RAZ. FAZA 2 (LLM): dla slajdu Z
    timestampem komentarz prowadzącego ze streszczenia segmentów jego okna; slajd BEZ timestampu →
    sekcja z obrazem + TEKST/OPIS + adnotacja (brak mapy czasowej).

    Egzekucja granicy prywatności jest podwójna dla OBU tras (VLM i LLM): :func:`resolve_route`
    wybiera trasę wg ``cloud_ok``, a :func:`assert_route_allowed` blokuje wrażliwy materiał na
    trasie chmurowej — rozstrzygane RAZ na job. ``replace`` na metadanych nie zeruje innych pól.
    """

    def handler(job: Job, progress: _ProgressCb) -> None:
        if job.recording_id is None:
            raise ValueError("notes: brak recording_id w zadaniu")
        material = store.get_material(job.recording_id)
        if material is None:
            raise ValueError(f"notes: materiał {job.recording_id} nie istnieje")
        folder, meta = material
        if not meta.slides:
            raise ValueError("notes: brak slajdów (najpierw podłącz slajdy)")
        if meta.transcript_status != "done" or not meta.transcript_json:
            raise ValueError("notes: brak transkryptu (najpierw transkrypcja)")

        vlm_route = resolve_route(
            cloud_ok=meta.cloud_ok, local_model=vlm_local, cloud_model=vlm_cloud
        )
        assert_route_allowed(vlm_route, cloud_ok=meta.cloud_ok)  # ostatnia linia obrony (VLM)
        llm_route = resolve_route(
            cloud_ok=meta.cloud_ok, local_model=llm_local, cloud_model=llm_cloud
        )
        assert_route_allowed(llm_route, cloud_ok=meta.cloud_ok)  # ostatnia linia obrony (LLM)

        slides = list(meta.slides)
        segments = read_transcript_segments(folder / meta.transcript_json)
        windows = pair_slides_with_segments(slides, segments)
        windows_by_index = {w.slide.index: w for w in windows}

        # Postęp: wykonane / (N_vlm + N_llm). N_llm = liczba slajdów Z timestampem (tylko one idą
        # przez LLM); slajdy bez timestampu nie liczą się do mianownika (nie wołają modelu).
        step = _StepProgress(progress, total=len(slides) + len(windows))

        analyses = _run_slide_analysis(vision_client, vlm_route, folder, slides, step)
        sections = _run_notes_commentary(
            summary_client, llm_route, slides, windows_by_index, analyses, step
        )
        _write_human_md(folder / NOTES_FILENAME, _assemble_notes(meta, sections))
        _persist_notes(store, folder, meta)
        progress(1.0)

    return handler


class _StepProgress:
    """Monotoniczny licznik kroków → ``jobs.progress`` (wykonane / total, sufit 0.99 do finału)."""

    def __init__(self, progress: _ProgressCb, *, total: int) -> None:
        self._progress = progress
        self._total = total
        self._done = 0

    def __call__(self) -> None:
        self._done += 1
        self._progress(min(0.99, self._done / self._total) if self._total else 0.99)


def _run_slide_analysis(
    vision_client: VisionClient,
    route: ModelRoute,
    folder: Path,
    slides: list[Slide],
    step: _StepProgress,
) -> dict[int, SlideAnalysis]:
    """FAZA 1: analiza VLM każdego slajdu, przyrostowo do ``slides_analysis.json`` (wznowienie).

    Wczytuje dotychczasowe analizy (ponowny job po błędzie pomija już gotowe slajdy), analizuje
    brakujące i po KAŻDYM przepisuje cały plik (praca częściowa chroniona). Wszystkie wywołania
    VLM idą tu POD RZĄD — dopiero potem faza 2, żeby Ollama ładowała model VLM tylko raz.
    """
    analysis_file = folder / SLIDES_ANALYSIS_FILENAME
    slides_dir = folder / SLIDES_DIRNAME
    analyses = _load_slide_analyses(analysis_file)
    for slide in slides:
        if slide.index not in analyses:
            analyses[slide.index] = vision_client.analyze_slide(slides_dir / slide.filename, route)
            _write_slide_analyses(analysis_file, analyses)  # przyrostowo po każdym slajdzie
        step()
    return analyses


def _run_notes_commentary(
    summary_client: SummaryClient,
    route: ModelRoute,
    slides: list[Slide],
    windows_by_index: dict[int, SlideWindow],
    analyses: dict[int, SlideAnalysis],
    step: _StepProgress,
) -> list[str]:
    """FAZA 2: sekcja per slajd. Slajd Z timestampem → komentarz LLM z okna; bez → adnotacja."""
    sections: list[str] = []
    for slide in slides:
        analysis = analyses.get(slide.index, SlideAnalysis("", "", ""))
        window = windows_by_index.get(slide.index)
        if window is None:
            sections.append(_untimed_section(slide, analysis))
            continue
        commentary = _slide_commentary(summary_client, route, window, analysis)
        sections.append(_timed_section(slide, analysis, window, commentary))
        step()
    return sections


def _slide_commentary(
    summary_client: SummaryClient,
    route: ModelRoute,
    window: SlideWindow,
    analysis: SlideAnalysis,
) -> str | None:
    """Komentarz prowadzącego dla slajdu (LLM) z analizy slajdu + tekstu segmentów okna.

    Okno bez segmentów (slajd z timestampem, ale bez mowy w tym czasie) → ``None`` bez wywołania
    modelu (nic do streszczenia). W przeciwnym razie jeden request do istniejącego klienta
    streszczeń; instrukcja formatu (Komentarz + Najważniejsze punkty) jest w treści usera.
    """
    segments_text = " ".join(s.text.strip() for s in window.segments).strip()
    if not segments_text:
        return None
    return summary_client.summarize(_commentary_prompt(analysis, segments_text), route)


def _commentary_prompt(analysis: SlideAnalysis, segments_text: str) -> str:
    """Instrukcja LLM: z analizy slajdu + transkryptu okna → komentarz + najważniejsze punkty."""
    return (
        "Na podstawie analizy slajdu wykładu i fragmentu transkryptu z jego czasu napisz notatkę "
        "po polsku. Zwróć DOKŁADNIE w tym formacie Markdown (bez nagłówka slajdu):\n"
        "### Komentarz prowadzącego\n<2-4 zdania: co prowadzący mówi w tym fragmencie>\n"
        "### Najważniejsze punkty\n- <punkt>\n- <punkt>\n(2-5 punktów łączących treść slajdu z "
        "komentarzem)\n\n"
        f"ANALIZA SLAJDU:\nTytuł: {analysis.title}\nTekst: {analysis.text}\n"
        f"Opis: {analysis.description}\n\n"
        f"TRANSKRYPT (ten zakres czasu):\n{segments_text}"
    )


def _timed_section(
    slide: Slide, analysis: SlideAnalysis, window: SlideWindow, commentary: str | None
) -> str:
    """Sekcja slajdu Z timestampem: nagłówek + czas + obraz + komentarz LLM (wg formatu z CELU)."""
    body = commentary or "_(brak transkryptu w tym oknie — bez komentarza prowadzącego)_"
    return (
        f"{_slide_heading(slide, analysis)}\n"
        f"Czas: {_time_range(window.start_s, window.end_s)}\n"
        f"{_slide_image(slide)}\n\n"
        f"{body}\n"
    )


def _untimed_section(slide: Slide, analysis: SlideAnalysis) -> str:
    """Sekcja slajdu BEZ timestampu: obraz + TEKST/OPIS z VLM + adnotacja o braku mapy czasowej."""
    parts = [_slide_heading(slide, analysis), _slide_image(slide)]
    if analysis.text:
        parts.append(f"### Tekst slajdu\n{analysis.text}")
    if analysis.description:
        parts.append(f"### Opis\n{analysis.description}")
    parts.append("_(brak mapy czasowej — bez komentarza prowadzącego)_")
    return "\n\n".join(parts) + "\n"


def _slide_heading(slide: Slide, analysis: SlideAnalysis) -> str:
    """Nagłówek sekcji ``## Slajd N — <tytuł>`` (bez tytułu, gdy VLM go nie zwrócił)."""
    base = f"## Slajd {slide.index}"
    return f"{base} — {analysis.title}" if analysis.title else base


def _slide_image(slide: Slide) -> str:
    """Obraz slajdu ze ścieżką WZGLĘDNĄ (``slides/...``) — działa po przeniesieniu folderu."""
    return f"![Slajd {slide.index}](slides/{slide.filename})"


def _time_range(start_s: int, end_s: int | None) -> str:
    """Zakres czasu okna ``HH:MM:SS-HH:MM:SS``; ostatni slajd (``end_s is None``) → „do końca"."""
    start = _hms(start_s)
    return f"{start} do końca" if end_s is None else f"{start}-{_hms(end_s)}"


def _hms(seconds: float) -> str:
    """Znacznik ``HH:MM:SS`` z sekund (ujemne → 00:00:00)."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _assemble_notes(meta: MaterialMetadata, sections: list[str]) -> str:
    """Składa ``notes.md``: nagłówek materiału (tytuł, data, prowadzący) + sekcje per slajd."""
    header = f"# {meta.title}"
    bits: list[str] = []
    if meta.created_at:
        bits.append(f"Data: {meta.created_at[:10]}")
    if meta.presenter:
        bits.append(f"Prowadzący: {meta.presenter}")
    if bits:
        header += "\n" + "  ·  ".join(bits)
    return f"{header}\n\n" + "\n".join(sections)


def _load_slide_analyses(path: Path) -> dict[int, SlideAnalysis]:
    """Wczytuje analizy z ``slides_analysis.json`` (wznowienie fazy 1); zepsuty/brak → pusty."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    result: dict[int, SlideAnalysis] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("index"), int):
                result[item["index"]] = SlideAnalysis(
                    title=str(item.get("title", "")),
                    text=str(item.get("text", "")),
                    description=str(item.get("description", "")),
                )
    return result


def _write_slide_analyses(path: Path, analyses: dict[int, SlideAnalysis]) -> None:
    """Zapisuje analizy do ``slides_analysis.json`` (BEZ BOM — plik maszynowy; sort po indeksie)."""
    payload = [
        {"index": i, "title": a.title, "text": a.text, "description": a.description}
        for i, a in sorted(analyses.items())
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _persist_notes(store: RecordingStore, folder: Path, meta: MaterialMetadata) -> None:
    """Zapis statusów notatki: metadata.json (źródło prawdy) + indeks SQLite (round-trip)."""
    updated = replace(meta, notes_status="done", notes_path=NOTES_FILENAME)
    write_metadata(folder, updated)
    store.upsert_material(folder, updated)


def enqueue_notes(
    store: RecordingStore,
    jobs: JobStore,
    recording_id: int,
    *,
    vlm_local: str | None,
    vlm_cloud: str | None,
    llm_local: str | None,
    llm_cloud: str | None,
) -> int:
    """Kolejkuje notatkę: odmawia bez slajdów/transkryptu, dobiera linię wg tras (VLM+LLM).

    Odmowa (``ValueError``) gdy materiał nie istnieje, nie ma slajdów („najpierw podłącz slajdy")
    albo ``transcript_status != done`` („najpierw transkrypcja"). Linia z :func:`notes_lane`
    (GPU, gdy którakolwiek trasa lokalna). Zwraca id joba.
    """
    material = store.get_material(recording_id)
    if material is None:
        raise ValueError(f"notes: materiał {recording_id} nie istnieje")
    _folder, meta = material
    if not meta.slides:
        raise ValueError("Najpierw podłącz slajdy — brak slajdów do notatki.")
    if meta.transcript_status != "done":
        raise ValueError("Najpierw transkrypcja — brak transkryptu do notatki.")
    vlm_route = resolve_route(cloud_ok=meta.cloud_ok, local_model=vlm_local, cloud_model=vlm_cloud)
    llm_route = resolve_route(cloud_ok=meta.cloud_ok, local_model=llm_local, cloud_model=llm_cloud)
    return jobs.enqueue(
        JOB_NOTES, recording_id=recording_id, payload={"lane": notes_lane(vlm_route, llm_route)}
    )


def make_import_handler(engine: ImporterEngine) -> _Handler:
    """Handler importu: payload (źródło + katalog biblioteki + metadane) → ImporterEngine."""

    def handler(job: Job, progress: _ProgressCb) -> None:
        payload = job.payload
        src = Path(str(payload["src"]))
        library_root = Path(str(payload["library_root"]))
        engine.import_file(
            src,
            library_root,
            lambda frac, _msg: progress(frac),
            title=payload.get("title"),
            category=payload.get("category"),
            tags=list(payload.get("tags") or []),
        )

    return handler


def make_download_handler(engine: DownloaderEngine) -> _Handler:
    """Handler pobierania: payload (URL + katalog + opcje/metadane) → DownloaderEngine.

    ``cookies_browser`` przekazywane TYLKO gdy użytkownik świadomie wybrał sesję przeglądarki
    (opt-in) — poświadczeń aplikacja nie widzi. ``cloud_ok`` z profilu źródła (domyślnie False).
    Błąd yt-dlp → wyjątek → kolejka zapisuje POWÓD (stderr) do ``jobs.error``.
    """

    def handler(job: Job, progress: _ProgressCb) -> None:
        payload = job.payload
        url = str(payload["url"])
        library_root = Path(str(payload["library_root"]))
        engine.download(
            url,
            library_root,
            lambda frac, _msg: progress(frac),
            audio_only=bool(payload.get("audio_only", False)),
            cookies_browser=payload.get("cookies_browser") or None,
            title=payload.get("title"),
            category=payload.get("category"),
            tags=list(payload.get("tags") or []),
            organizer=payload.get("organizer"),
            presenter=payload.get("presenter"),
            cloud_ok=bool(payload.get("cloud_ok", False)),
            description=payload.get("description"),
        )

    return handler
