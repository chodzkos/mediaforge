"""Magazyn zadań nad tabelą ``jobs`` (status, progress, error, retry).

Czysty ``sqlite3`` (bez Qt). Każda operacja otwiera krótkożyciowe połączenie
przez :func:`core.library.db.connect`, więc store jest bezpieczny wątkowo i
nadaje się pod pulę wątków kolejki. Schemat tabeli tworzy migracja biblioteki.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from sqlite3 import Row
from typing import Any

from mediaforge.core.library.db import connect


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Job:
    """Pojedyncze zadanie kolejki (wiersz tabeli ``jobs``)."""

    id: int
    recording_id: int | None
    job_type: str
    status: JobStatus
    progress: float
    error_message: str | None
    retry_count: int
    max_retries: int
    payload: dict[str, Any]
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_job(row: Row) -> Job:
    raw_payload = row["payload"]
    payload: dict[str, Any] = json.loads(raw_payload) if raw_payload else {}
    return Job(
        id=int(row["id"]),
        recording_id=row["recording_id"],
        job_type=str(row["job_type"]),
        status=JobStatus(row["status"]),
        progress=float(row["progress"]),
        error_message=row["error_message"],
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        payload=payload,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class JobStore:
    """CRUD + przejścia stanów dla tabeli ``jobs`` (z logiką retry)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def enqueue(
        self,
        job_type: str,
        *,
        recording_id: int | None = None,
        payload: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> int:
        """Dodaje zadanie w stanie ``pending`` i zwraca jego id."""
        now = _now()
        conn = connect(self.path)
        try:
            cur = conn.execute(
                "INSERT INTO jobs (recording_id, job_type, status, progress, "
                "max_retries, payload, created_at, updated_at) "
                "VALUES (?, ?, 'pending', 0.0, ?, ?, ?, ?)",
                (
                    recording_id,
                    job_type,
                    max_retries,
                    json.dumps(payload or {}),
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def claim_next(self) -> Job | None:
        """Atomowo bierze najstarsze ``pending`` zadanie i ustawia je na ``running``."""
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            return _row_to_job(updated)
        finally:
            conn.close()

    def recover_stale(self) -> int:
        """Wraca zawieszone ``running`` do ``pending`` i zwraca liczbę odzyskanych zadań.

        Wołane raz przy starcie aplikacji, ZANIM ruszy dispatcher — zadania przerwane
        zamknięciem/awarią wracają do kolejki (``retry_count`` bez zmian: przerwanie to nie
        porażka handlera). Bez tego proces, który padł w trakcie handlera, zostawia wiersz
        ``running`` na zawsze — nikt go już nie dokończy.
        """
        conn = connect(self.path)
        try:
            cur = conn.execute(
                "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def set_progress(self, job_id: int, progress: float) -> None:
        """Aktualizuje postęp zadania (0..1)."""
        clamped = max(0.0, min(1.0, progress))
        conn = connect(self.path)
        try:
            conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?",
                (clamped, _now(), job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_done(self, job_id: int) -> None:
        """Oznacza zadanie jako ukończone (progress = 1.0)."""
        conn = connect(self.path)
        try:
            conn.execute(
                "UPDATE jobs SET status = 'done', progress = 1.0, "
                "error_message = NULL, updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_failed(self, job_id: int, error: str) -> JobStatus:
        """Obsługuje porażkę: ponawia (``pending``) póki jest budżet retry, inaczej ``failed``.

        Zwraca wynikowy status zadania po decyzji retry.
        """
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT retry_count, max_retries FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return JobStatus.FAILED
            retry_count = int(row["retry_count"])
            max_retries = int(row["max_retries"])
            if retry_count < max_retries:
                new_status = JobStatus.PENDING
                conn.execute(
                    "UPDATE jobs SET status = 'pending', retry_count = ?, "
                    "error_message = ?, updated_at = ? WHERE id = ?",
                    (retry_count + 1, error, _now(), job_id),
                )
            else:
                new_status = JobStatus.FAILED
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error_message = ?, "
                    "updated_at = ? WHERE id = ?",
                    (error, _now(), job_id),
                )
            conn.commit()
            return new_status
        finally:
            conn.close()

    def get(self, job_id: int) -> Job | None:
        """Zwraca zadanie po id albo ``None``."""
        conn = connect(self.path)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        """Lista zadań (opcjonalnie filtrowana po statusie), najstarsze pierwsze."""
        conn = connect(self.path)
        try:
            if status is None:
                rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY id", (status.value,)
                ).fetchall()
            return [_row_to_job(r) for r in rows]
        finally:
            conn.close()

    def counts(self) -> dict[JobStatus, int]:
        """Liczność zadań wg statusu (do paska/podsumowania kolejki)."""
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            return {JobStatus(r["status"]): int(r["n"]) for r in rows}
        finally:
            conn.close()
