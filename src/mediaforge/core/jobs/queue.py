"""Pula wątków wykonująca zadania z :class:`JobStore`.

Executor to ``concurrent.futures.ThreadPoolExecutor`` (std-lib) — nie QThread —
bo ``core`` nie importuje Qt. Handler per typ zadania dostaje :class:`Job` i
callback postępu (aktualizuje store). Wyjątek handlera → ``mark_failed`` (retry
liczy store). GUI obserwuje stan kolejki przez odpytywanie store (np. ``QTimer``).

**Linie (lanes) — serializacja zadań GPU.** Każdy typ zadania może mieć dedykowaną
linię z własnym ``max_workers`` (osobny executor). Transkrypcja (whisper.cpp na GPU)
biegnie z ``max_workers=1`` — jeden model w VRAM naraz (zasada „sequential VRAM" z
CLAUDE.md, która później obejmie VLM/LLM). Import (I/O + ffmpeg, bez modelu) ma własną
linię i nie czeka na GPU. Typy bez własnej linii idą wspólną pulą ``workers``.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from mediaforge.core.jobs.store import Job, JobStatus, JobStore

logger = logging.getLogger("mediaforge")

# Handler zadania: dostaje Job i callback postępu ``progress(frakcja_0_1)``.
JobHandler = Callable[[Job, Callable[[float], None]], None]

_DEFAULT_LANE = "_default"


class JobQueue:
    """Wykonuje zadania ze store w puli wątków; handler dobierany po ``job_type``.

    Tryby użycia:

    * :meth:`process_pending` — synchroniczne opróżnienie kolejki (CLI/testy);
    * :meth:`start`/:meth:`stop` — dispatcher w tle (GUI), pollujący nowe zadania.

    **Linie współdzielone (sequential VRAM).** ``lanes`` to mapa NAZWA→max_workers
    (osobny executor na linię), a ``routes`` przypisuje ``job_type`` do linii. Wiele typów
    GPU (transkrypcja, później VLM/LLM) wskazuje TĘ SAMĄ linię ``gpu`` (max_workers=1) →
    jeden executor → tylko jeden model w VRAM naraz, niezależnie od typu zadania. To nie
    serializacja „typ-względem-siebie", lecz globalna serializacja całej linii GPU. Typy bez
    trasy idą wspólną pulą ``workers``.

    Args:
        store: magazyn zadań (tabela ``jobs``).
        workers: rozmiar wspólnej puli dla typów bez trasy.
        lanes: mapa ``nazwa_linii -> max_workers`` (jeden executor na linię).
        routes: mapa ``job_type -> nazwa_linii``.
    """

    def __init__(
        self,
        store: JobStore,
        *,
        workers: int = 2,
        lanes: dict[str, int] | None = None,
        routes: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._workers = workers
        self._lanes = dict(lanes or {})
        self._routes = dict(routes or {})
        self._handlers: dict[str, JobHandler] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Rejestruje handler dla danego typu zadania."""
        self._handlers[job_type] = handler

    def _lane_of(self, job: Job) -> str:
        """Nazwa linii zadania: override z ``payload['lane']`` > trasa typu > wspólna pula.

        Zadanie może wskazać linię jawnie w payloadzie (decyzja w momencie enqueue — np.
        streszczenie chmurowe idzie na linię I/O, żeby nie blokować GPU, mimo że domyślna
        trasa typu ``summarize`` to linia GPU dla wariantu lokalnego). Bez override obowiązuje
        trasa po typie: wspólna linia GPU = globalna serializacja modeli (transkrypcja +
        VLM/LLM na tej samej linii)."""
        lane = job.payload.get("lane")
        if isinstance(lane, str) and lane:
            return lane
        return self._routes.get(job.job_type, _DEFAULT_LANE)

    def _lane_workers(self, lane: str) -> int:
        return self._lanes.get(lane, self._workers)

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
        """Opróżnia bieżące ``pending`` zadania; zwraca liczbę wykonań.

        Każda linia biegnie w osobnym executorze z własnym ``max_workers`` — typ z linią
        1-wątkową (np. transkrypcja) jest serializowany. Zadania ponowione (retry →
        ``pending``) zostają na następny przebieg, więc jedno wywołanie się nie zapętla.
        """
        jobs: list[Job] = []
        while True:
            job = self._store.claim_next()
            if job is None:
                break
            jobs.append(job)
        if not jobs:
            return 0
        groups: dict[str, list[Job]] = defaultdict(list)
        for job in jobs:
            groups[self._lane_of(job)].append(job)
        for lane, group in groups.items():
            with ThreadPoolExecutor(max_workers=self._lane_workers(lane)) as pool:
                list(pool.map(self._run_job, group))
        return len(jobs)

    def start(self, poll_interval: float = 0.2) -> None:
        """Startuje dispatcher w tle (pollujący store i wykonujący zadania per linia)."""
        if self._thread is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            executors: dict[str, ThreadPoolExecutor] = {}
            try:
                while not self._stop.is_set():
                    job = self._store.claim_next()
                    if job is None:
                        self._stop.wait(poll_interval)
                        continue
                    lane = self._lane_of(job)
                    pool = executors.get(lane)
                    if pool is None:
                        pool = ThreadPoolExecutor(max_workers=self._lane_workers(lane))
                        executors[lane] = pool
                    pool.submit(self._run_job, job)
            finally:
                for pool in executors.values():
                    pool.shutdown(wait=True)

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
