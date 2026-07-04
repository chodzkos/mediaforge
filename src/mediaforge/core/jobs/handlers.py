"""Handlery zadań kolejki (Qt-free) — transkrypcja i import.

Łączą kolejkę (:mod:`core.jobs`) z silnikami (transkrypcja whisper.cpp, import A/V) i
biblioteką (``metadata.json`` = źródło prawdy + indeks SQLite). Każdy handler to domknięcie
``(job, progress) -> None`` rejestrowane w :class:`JobQueue`. Wyjątek handlera → kolejka
oznacza job jako błędny (status + komunikat). Bez Qt — GUI tylko odpytuje statusy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from mediaforge.core.ai.routing import ModelRoute, assert_route_allowed, resolve_route
from mediaforge.core.ai.summarize import SummaryClient, read_transcript_text
from mediaforge.core.ai.transcribe import TranscribeOptions, TranscriptionBackend
from mediaforge.core.engines.download_engine import DownloaderEngine
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs.store import Job, JobStore
from mediaforge.core.library.material import write_metadata
from mediaforge.core.library.recordings import RecordingStore

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


def make_summarize_handler(
    store: RecordingStore,
    client: SummaryClient,
    *,
    local_model: str | None,
    cloud_model: str | None,
) -> _Handler:
    """Handler streszczenia: materiał → resolve_route → assert → gateway → summary.md + statusy.

    Egzekucja granicy prywatności jest podwójna: :func:`resolve_route` wybiera trasę wg
    ``cloud_ok`` materiału, a :func:`assert_route_allowed` (ostatnia linia obrony tuż przed
    wysyłką) blokuje wrażliwy materiał na trasie chmurowej. Zapis: ``summary.md`` w folderze
    materiału (źródło prawdy) + ``summary_status='done'`` + ścieżka w metadanych; ``replace``
    na istniejących metadanych nie zeruje pozostałych pól.
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
        text = read_transcript_text(folder / meta.transcript_json)
        summary = client.summarize(text, route)  # GatewayError → job.error z base_url
        summary_path = folder / "summary.md"
        # BOM (utf-8-sig) WYŁĄCZNIE dla plików czytanych przez człowieka w zewnętrznych
        # aplikacjach: summary.md otwiera się w Calibre/czytnikach windowsowych, które bez BOM
        # zgadują cp1250 → krzaki w polskich znakach. NIE dodawaj BOM do transcript.json /
        # metadata.json (łamie parsery JSON, RFC 8259) ani .srt (pisze je whisper-cli).
        summary_path.write_text(summary, encoding="utf-8-sig")

        updated = replace(meta, summary_status="done", summary_path=summary_path.name)
        write_metadata(folder, updated)  # metadata.json = źródło prawdy
        store.upsert_material(folder, updated)  # indeks SQLite
        progress(1.0)

    return handler


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
