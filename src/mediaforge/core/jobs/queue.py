"""Pula wątków wykonująca zadania z :class:`JobStore`.

Executor to ``concurrent.futures.ThreadPoolExecutor`` (std-lib) — nie QThread —
bo ``core`` nie importuje Qt. Handler per typ zadania dostaje :class:`Job` i
callback postępu (aktualizuje store). Wyjątek handlera → ``mark_failed`` (retry
liczy store). GUI obserwuje stan kolejki przez odpytywanie store (np. ``QTimer``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from mediaforge.core.jobs.store import Job, JobStatus, JobStore

logger = logging.getLogger("mediaforge")

# Handler zadania: dostaje Job i callback postępu ``progress(frakcja_0_1)``.
JobHandler = Callable[[Job, Callable[[float], None]], None]


class JobQueue:
    """Wykonuje zadania ze store w puli wątków; handler dobierany po ``job_type``.

    Tryby użycia:

    * :meth:`process_pending` — synchroniczne opróżnienie kolejki (CLI/testy);
    * :meth:`start`/:meth:`stop` — dispatcher w tle (GUI), pollujący nowe zadania.

    Args:
        store: magazyn zadań (tabela ``jobs``).
        workers: rozmiar puli wątków.
    """

    def __init__(self, store: JobStore, *, workers: int = 2) -> None:
        self._store = store
        self._workers = workers
        self._handlers: dict[str, JobHandler] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Rejestruje handler dla danego typu zadania."""
        self._handlers[job_type] = handler

    def _run_job(self, job: Job) -> None:
        """Uruchamia handler zadania; sukces → done, wyjątek/brak handlera → failed."""
        handler = self._handlers.get(job.job_type)
        if handler is None:
            self._store.mark_failed(job.id, f"Brak handlera dla typu: {job.job_type}")
            return
        try:
            handler(job, lambda frac: self._store.set_progress(job.id, frac))
        except Exception as exc:
            status = self._store.mark_failed(job.id, str(exc))
            logger.warning("Zadanie %s nie powiodło się (%s): %s", job.id, status, exc)
        else:
            self._store.mark_done(job.id)

    def process_pending(self) -> int:
        """Opróżnia bieżące ``pending`` zadania w puli wątków; zwraca liczbę wykonań.

        Zadania ponowione (retry → ``pending``) zostają na następny przebieg, więc
        jedno wywołanie nie zapętla się na trwale failującym zadaniu.
        """
        jobs: list[Job] = []
        while True:
            job = self._store.claim_next()
            if job is None:
                break
            jobs.append(job)
        if not jobs:
            return 0
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            list(pool.map(self._run_job, jobs))
        return len(jobs)

    def start(self, poll_interval: float = 0.2) -> None:
        """Startuje dispatcher w tle (pollujący store i wykonujący zadania)."""
        if self._thread is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                while not self._stop.is_set():
                    job = self._store.claim_next()
                    if job is None:
                        self._stop.wait(poll_interval)
                        continue
                    pool.submit(self._run_job, job)

        self._thread = threading.Thread(target=_loop, name="mediaforge-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Zatrzymuje dispatcher i czeka na zakończenie wątku."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def pending_count(self) -> int:
        """Liczba zadań oczekujących (do paska statusu)."""
        return self._store.counts().get(JobStatus.PENDING, 0)
