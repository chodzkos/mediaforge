"""Handlery kolejki: transkrypcja i import jako joby (synchroniczny executor, atrapy)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mediaforge.core.ai.transcribe import (
    TranscribeOptions,
    Transcript,
    TranscriptionResult,
)
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs import JobQueue, JobStatus, JobStore
from mediaforge.core.jobs.handlers import (
    JOB_IMPORT,
    JOB_TRANSCRIBE,
    make_import_handler,
    make_transcribe_handler,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    Database(db).migrate()
    return db


class _FakeBackend:
    """Backend transkrypcji-atrapa: pisze .json/.srt do folderu, zwraca runtime cuda."""

    name = "fake"

    def transcribe(
        self,
        source: Path,
        out_dir: Path,
        opts: TranscribeOptions,
        *,
        on_progress: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        if on_progress is not None:
            on_progress(50)  # symulacja postępu w połowie
        json_path = out_dir / f"{source.stem}.json"
        srt_path = out_dir / f"{source.stem}.srt"
        json_path.write_text("{}", encoding="utf-8")
        srt_path.write_text("1\n", encoding="utf-8")
        return TranscriptionResult(
            transcript=Transcript(language="pl", model="fake"),
            runtime="cuda",
            json_path=json_path,
            srt_path=srt_path,
        )


def _seed_material(store: RecordingStore, lib: Path, title: str) -> tuple[int, Path]:
    folder = lib / title
    meta = MaterialMetadata(title=title, created_at="t", audio_path=f"{title}.wav")
    write_metadata(folder, meta)
    (folder / f"{title}.wav").write_bytes(b"AUDIO")
    rec_id = store.upsert_material(folder, read_metadata(folder))
    return rec_id, folder


def test_transcribe_job_writes_status_and_files(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_material(store, tmp_path / "lib", "Wyklad")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FakeBackend()))
    job_id = JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=rec_id)

    assert queue.process_pending() == 1
    assert JobStore(db).get(job_id).status is JobStatus.DONE  # type: ignore[union-attr]

    # metadata.json (źródło prawdy) zaktualizowane.
    meta = read_metadata(folder)
    assert meta.transcript_status == "done"
    assert meta.transcript_json == "Wyklad.json" and meta.transcript_srt == "Wyklad.srt"
    # SQLite zsynchronizowane.
    material = store.get_material(rec_id)
    assert material is not None and material[1].transcript_status == "done"
    # rescan zachowuje status (nie zeruje).
    store.rescan(tmp_path / "lib")
    again = store.get_material(rec_id)
    assert again is not None and again[1].transcript_status == "done"


def test_transcribe_handler_forwards_progress(tmp_path: Path) -> None:
    """Handler przekazuje on_progress(pct) → callback postępu kolejki jako frakcję (pct/100)."""
    from mediaforge.core.jobs.store import Job

    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _ = _seed_material(store, tmp_path / "lib", "M")
    handler = make_transcribe_handler(store, _FakeBackend())

    seen: list[float] = []
    job = Job(
        id=1,
        recording_id=rec_id,
        job_type=JOB_TRANSCRIBE,
        status=JobStatus.RUNNING,
        progress=0.0,
        error_message=None,
        retry_count=0,
        max_retries=0,
        payload={},
        created_at="t",
        updated_at="t",
    )
    handler(job, seen.append)
    assert 0.5 in seen  # on_progress(50) → progress(0.5)


def test_transcribe_does_not_reset_other_material(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    a_id, _ = _seed_material(store, tmp_path / "lib", "A")
    b_id, _ = _seed_material(store, tmp_path / "lib", "B")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FakeBackend()))
    JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=a_id)
    queue.process_pending()

    other = store.get_material(b_id)
    assert other is not None and other[1].transcript_status == "none"  # nietknięty


def test_transcribe_job_fails_for_material_without_folder(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    # Wiersz bez folderu (np. sprzed S2) → get_material zwraca None → job błędny, nie crash.
    rec_id = store.create("bez folderu")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FakeBackend()))
    job_id = JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=rec_id, max_retries=0)

    queue.process_pending()
    failed = JobStore(db).get(job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error_message and str(rec_id) in failed.error_message


def _fake_copy(src: Path, dest: Path) -> object:
    dest.write_bytes(b"FAKE")
    return dest


def test_import_job_creates_material(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    src = tmp_path / "clip.mp3"
    src.write_bytes(b"SRC")
    engine = ImporterEngine(
        store=store, runner=lambda c: 0, probe_runner=lambda c: "12\n", copy_fn=_fake_copy
    )

    queue = JobQueue(JobStore(db), lanes={JOB_IMPORT: 2})
    queue.register(JOB_IMPORT, make_import_handler(engine))
    JobStore(db).enqueue(
        JOB_IMPORT,
        payload={
            "src": str(src),
            "library_root": str(tmp_path / "lib"),
            "category": "Podcast",
            "tags": ["audio"],
        },
    )

    assert queue.process_pending() == 1
    materials = store.list_materials()
    assert len(materials) == 1
    assert materials[0][2].title == "clip" and materials[0][2].category == "Podcast"
