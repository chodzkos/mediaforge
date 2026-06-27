"""Kolejka zadań mediaforge — tabela SQLite ``jobs`` + pula wątków.

Bez Celery/Redis: trwałość zadań w bazie biblioteki (:mod:`core.library`),
wykonanie w puli wątków ze standardowej biblioteki. **Świadomie nie QThread** —
``core`` nie importuje Qt (twarda reguła CLAUDE.md), więc executor to
``concurrent.futures.ThreadPoolExecutor``; GUI jest adapterem Qt nad tym
(np. ``QTimer`` odświeżający widok kolejki). Retry liczony w :mod:`.store`.
"""

from __future__ import annotations

from mediaforge.core.jobs.queue import JobHandler, JobQueue
from mediaforge.core.jobs.store import Job, JobStatus, JobStore

__all__ = ["Job", "JobHandler", "JobQueue", "JobStatus", "JobStore"]
