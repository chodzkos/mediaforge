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

from mediaforge.core.ai.transcribe import TranscribeOptions, TranscriptionBackend
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs.store import Job
from mediaforge.core.library.material import write_metadata
from mediaforge.core.library.recordings import RecordingStore

# Typy zadań (job_type) używane przez kolejkę.
JOB_TRANSCRIBE = "transcribe"
JOB_IMPORT = "import"

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
        result = backend.transcribe(folder / relative, folder, opts)
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
