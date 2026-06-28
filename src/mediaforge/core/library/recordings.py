"""Magazyn nagrań nad tabelą ``recordings`` (wpis materiału + statusy).

Czysty ``sqlite3`` (bez Qt), krótkożyciowe połączenia przez
:func:`core.library.db.connect` — bezpieczny wątkowo, jak :mod:`core.jobs.store`.
Schemat tabeli tworzy migracja biblioteki (:mod:`core.library.db`). Folder materiału
(„jeden materiał = jeden folder") jest źródłem prawdy; tabela to indeks nad nim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from sqlite3 import Row

from mediaforge.core.library.db import connect


class RecordingStatus(StrEnum):
    """Cykl życia materiału w bibliotece."""

    NEW = "new"
    RECORDING = "recording"
    RECORDED = "recorded"  # nagranie ukończone, pliki na dysku
    FAILED = "failed"


@dataclass(slots=True)
class Recording:
    """Wiersz tabeli ``recordings`` (materiał w bibliotece)."""

    id: int
    title: str
    source_type: str
    created_at: str
    status: RecordingStatus
    duration: float | None = None
    video_path: str | None = None
    audio_path: str | None = None
    category: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_recording(row: Row) -> Recording:
    return Recording(
        id=int(row["id"]),
        title=str(row["title"]),
        source_type=str(row["source_type"]),
        created_at=str(row["created_at"]),
        status=RecordingStatus(row["status"]),
        duration=row["duration"],
        video_path=row["video_path"],
        audio_path=row["audio_path"],
        category=row["category"],
    )


class RecordingStore:
    """CRUD dla tabeli ``recordings`` (tworzenie wpisu, aktualizacja statusu/ścieżek)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def create(
        self,
        title: str,
        *,
        source_type: str = "screen",
        status: RecordingStatus = RecordingStatus.RECORDED,
        duration: float | None = None,
        video_path: Path | str | None = None,
        audio_path: Path | str | None = None,
        category: str | None = None,
    ) -> int:
        """Wstawia wpis materiału i zwraca jego id (domyślnie status ``recorded``)."""
        conn = connect(self.path)
        try:
            cur = conn.execute(
                "INSERT INTO recordings (title, source_type, category, created_at, "
                "duration, video_path, audio_path, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    source_type,
                    category,
                    _now(),
                    duration,
                    str(video_path) if video_path is not None else None,
                    str(audio_path) if audio_path is not None else None,
                    status.value,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def set_status(self, recording_id: int, status: RecordingStatus) -> None:
        """Aktualizuje status materiału."""
        conn = connect(self.path)
        try:
            conn.execute(
                "UPDATE recordings SET status = ? WHERE id = ?",
                (status.value, recording_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, recording_id: int) -> Recording | None:
        """Zwraca materiał po id albo ``None``."""
        conn = connect(self.path)
        try:
            row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            return _row_to_recording(row) if row is not None else None
        finally:
            conn.close()

    def list_recordings(self, status: RecordingStatus | None = None) -> list[Recording]:
        """Lista materiałów (opcjonalnie po statusie), najnowsze pierwsze."""
        conn = connect(self.path)
        try:
            if status is None:
                rows = conn.execute("SELECT * FROM recordings ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recordings WHERE status = ? ORDER BY id DESC",
                    (status.value,),
                ).fetchall()
            return [_row_to_recording(r) for r in rows]
        finally:
            conn.close()
