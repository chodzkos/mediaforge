"""Kolejka zadań: store (status/progress/retry) + pula wątków."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mediaforge.core.jobs import Job, JobQueue, JobStatus, JobStore
from mediaforge.core.library import Database


def _store(tmp_path: Path) -> JobStore:
    db = Database(tmp_path / "library.sqlite3")
    db.migrate()
    return JobStore(db.path)


def test_enqueue_claim_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # recording_id=None: zadanie bez powiązanego nagrania (FK do recordings jest nullable).
    job_id = store.enqueue("transcribe", payload={"k": "v"})

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.payload == {"k": "v"}
    assert store.claim_next() is None  # nic więcej nie czeka

    store.mark_done(job_id)
    done = store.get(job_id)
    assert done is not None and done.status is JobStatus.DONE and done.progress == 1.0


def test_retry_then_fail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job_id = store.enqueue("flaky", max_retries=1)
    store.claim_next()

    assert store.mark_failed(job_id, "boom") is JobStatus.PENDING  # jest budżet retry
    after_first = store.get(job_id)
    assert after_first is not None and after_first.retry_count == 1

    store.claim_next()
    assert store.mark_failed(job_id, "boom again") is JobStatus.FAILED  # wyczerpany
    final = store.get(job_id)
    assert final is not None and final.error_message == "boom again"


def test_queue_processes_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seen: list[int] = []

    def handler(job: Job, progress: Callable[[float], None]) -> None:
        progress(0.5)
        seen.append(job.id)

    queue = JobQueue(store, workers=2)
    queue.register("work", handler)

    assert queue.process_pending() == 0  # pusta kolejka przechodzi smoke

    a = store.enqueue("work")
    b = store.enqueue("work")
    assert queue.process_pending() == 2
    assert set(seen) == {a, b}
    assert store.get(a) is not None and store.get(a).status is JobStatus.DONE  # type: ignore[union-attr]


def test_queue_marks_failure_on_handler_exception(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def boom(job: Job, progress: Callable[[float], None]) -> None:
        raise RuntimeError("nope")

    queue = JobQueue(store, workers=1)
    queue.register("explode", boom)
    job_id = store.enqueue("explode", max_retries=0)
    queue.process_pending()

    final = store.get(job_id)
    assert final is not None and final.status is JobStatus.FAILED
