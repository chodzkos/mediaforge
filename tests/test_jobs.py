"""Kolejka zadań: store (status/progress/retry) + pula wątków."""

from __future__ import annotations

import threading
import time
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


def test_recover_stale_returns_running_to_pending(tmp_path: Path) -> None:
    """Restart procesu: zawieszone ``running`` wraca do kolejki (retry_count bez zmian)."""
    store = _store(tmp_path)
    job_id = store.enqueue("transcribe")
    claimed = store.claim_next()
    assert claimed is not None and claimed.status is JobStatus.RUNNING

    # Nowy JobStore na TYM SAMYM pliku = symulacja restartu aplikacji.
    restarted = JobStore(store.path)
    assert restarted.recover_stale() == 1
    recovered = restarted.get(job_id)
    assert recovered is not None
    assert recovered.status is JobStatus.PENDING
    assert recovered.retry_count == 0  # przerwanie to nie porażka handlera
    # Dispatcher po restarcie znów je bierze.
    again = restarted.claim_next()
    assert again is not None and again.id == job_id


def test_recover_stale_empty_db_returns_zero(tmp_path: Path) -> None:
    """Brak zawieszonych ``running`` → 0 (czysta baza / same pending/done)."""
    store = _store(tmp_path)
    assert store.recover_stale() == 0


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


def _vram_guard_handler(
    lock: threading.Lock, active: dict[str, int]
) -> Callable[[Job, Callable[[float], None]], None]:
    def handler(job: Job, progress: Callable[[float], None]) -> None:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.02)  # okno na nakładkę, gdyby zadania szły równolegle
        with lock:
            active["now"] -= 1

    return handler


def test_gpu_lane_serializes_same_type(tmp_path: Path) -> None:
    """Linia GPU (max_workers=1) serializuje zadania jednego typu — jeden model w VRAM."""
    store = _store(tmp_path)
    lock = threading.Lock()
    active = {"now": 0, "max": 0}
    queue = JobQueue(store, workers=4, lanes={"gpu": 1}, routes={"transcribe": "gpu"})
    queue.register("transcribe", _vram_guard_handler(lock, active))
    for _ in range(4):
        store.enqueue("transcribe")

    assert queue.process_pending() == 4
    assert active["max"] == 1


def test_gpu_lane_serializes_across_types(tmp_path: Path) -> None:
    """KLUCZOWE: różne typy GPU na wspólnej linii ``gpu`` też się serializują (VLM vs transkrypcja).

    Tu jest sens współdzielonej linii: transkrypcja i przyszły VLM/LLM nie ładują dwóch modeli
    do VRAM naraz. Gdyby każdy typ miał własny executor, ten test by padł (max == 2).
    """
    store = _store(tmp_path)
    lock = threading.Lock()
    active = {"now": 0, "max": 0}
    handler = _vram_guard_handler(lock, active)
    queue = JobQueue(store, workers=4, lanes={"gpu": 1}, routes={"transcribe": "gpu", "vlm": "gpu"})
    queue.register("transcribe", handler)
    queue.register("vlm", handler)
    store.enqueue("transcribe")
    store.enqueue("vlm")
    store.enqueue("transcribe")
    store.enqueue("vlm")

    assert queue.process_pending() == 4
    assert active["max"] == 1  # nigdy dwa modele GPU naraz, NIEZALEŻNIE od typu


def test_default_routes_serialize_transcribe_and_summarize_on_gpu(tmp_path: Path) -> None:
    """DEFAULT_ROUTES: transkrypcja i streszczenie LOKALNE dzielą linię GPU → jeden model w VRAM.

    Sprawdza „tę jedną linię" z S4 (DEFAULT_ROUTES[JOB_SUMMARIZE] = GPU_LANE): streszczenie
    modelem lokalnym nie ładuje się do VRAM równolegle z transkrypcją (max == 1).
    """
    from mediaforge.core.jobs.handlers import (
        DEFAULT_LANES,
        DEFAULT_ROUTES,
        JOB_SUMMARIZE,
        JOB_TRANSCRIBE,
    )

    store = _store(tmp_path)
    lock = threading.Lock()
    active = {"now": 0, "max": 0}
    handler = _vram_guard_handler(lock, active)
    queue = JobQueue(store, workers=4, lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    queue.register(JOB_TRANSCRIBE, handler)
    queue.register(JOB_SUMMARIZE, handler)
    # Bez payload['lane'] → obie trasy typu wskazują GPU (streszczenie lokalne).
    store.enqueue(JOB_TRANSCRIBE)
    store.enqueue(JOB_SUMMARIZE)
    store.enqueue(JOB_TRANSCRIBE)
    store.enqueue(JOB_SUMMARIZE)

    assert queue.process_pending() == 4
    assert active["max"] == 1


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
