"""Handlery zadań kolejki (Qt-free) — transkrypcja i import.

Łączą kolejkę (:mod:`core.jobs`) z silnikami (transkrypcja whisper.cpp, import A/V) i
biblioteką (``metadata.json`` = źródło prawdy + indeks SQLite). Każdy handler to domknięcie
``(job, progress) -> None`` rejestrowane w :class:`JobQueue`. Wyjątek handlera → kolejka
oznacza job jako błędny (status + komunikat). Bez Qt — GUI tylko odpytuje statusy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from mediaforge.core.ai.chunking import Chunk, split_segments
from mediaforge.core.ai.routing import ModelRoute, assert_route_allowed, resolve_route
from mediaforge.core.ai.summarize import (
    TRUNCATED_MARK,
    GatewayError,
    SummaryClient,
    SummaryResult,
    map_prompt,
    part_section,
    read_transcript_segments,
    reduce_parts,
    summary_start_line,
)
from mediaforge.core.ai.transcribe import Segment, TranscribeOptions, TranscriptionBackend
from mediaforge.core.config import DEFAULT_SUMMARY_CHUNK_CHARS
from mediaforge.core.engines.download_engine import DownloaderEngine
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs.store import Job, JobStore
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore

logger = logging.getLogger("mediaforge")

SUMMARY_FILENAME = "summary.md"
SUMMARY_PARTS_FILENAME = "summary_parts.md"

# Typy zadań (job_type) używane przez kolejkę.
JOB_TRANSCRIBE = "transcribe"
JOB_IMPORT = "import"
JOB_SUMMARIZE = "summarize"
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
        final = _run_reduce(client, route, chunk_chars, summary_parts, progress)
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
) -> SummaryResult:
    """Faza REDUCE: sklej streszczenia cząstkowe w jedno (hierarchicznie, gdy trzeba).

    Postęp w paśmie [0.7, 1.0]: monotoniczny, krok po każdym wywołaniu reduce (liczba wywołań
    zależy od głębokości hierarchii, nieznanej z góry — stąd asymptotyczne zbliżanie do 1.0,
    domknięte ``progress(1.0)`` przez wołającego).
    """
    reduce_done = 0

    def reduce_call(user_content: str) -> SummaryResult:
        nonlocal reduce_done
        res = client.run(user_content, route)
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
