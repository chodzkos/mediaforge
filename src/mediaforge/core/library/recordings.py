"""Magazyn nagrań nad tabelą ``recordings`` (indeks materiałów + statusy).

Czysty ``sqlite3`` (bez Qt), krótkożyciowe połączenia przez
:func:`core.library.db.connect`. Folder materiału z ``metadata.json``
(:mod:`core.library.material`) jest **źródłem prawdy**; ta tabela to indeks nad nim —
:meth:`RecordingStore.upsert_material` synchronizuje metadane do SQLite (lista/filtry),
a :meth:`RecordingStore.to_metadata` czyta je z powrotem (round-trip folder ↔ SQLite).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from sqlite3 import Connection, Row

from mediaforge.core.library.db import connect
from mediaforge.core.library.material import MaterialMetadata


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


def _read_tags(conn: Connection, recording_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM tags WHERE recording_id = ? ORDER BY tag", (recording_id,)
    ).fetchall()
    return [str(r["tag"]) for r in rows]


def _write_tags(conn: Connection, recording_id: int, tags: list[str]) -> None:
    conn.execute("DELETE FROM tags WHERE recording_id = ?", (recording_id,))
    conn.executemany(
        "INSERT INTO tags (recording_id, tag) VALUES (?, ?)",
        [(recording_id, t) for t in sorted({t.strip() for t in tags if t.strip()})],
    )


def _row_to_metadata(row: Row, tags: list[str]) -> MaterialMetadata:
    return MaterialMetadata(
        title=str(row["title"]),
        created_at=str(row["created_at"]),
        source_type=str(row["source_type"]),
        source_url=row["source_url"],
        presenter=row["presenter"],
        organizer=row["organizer"],
        category=row["category"],
        tags=tags,
        duration=row["duration"],
        video_path=row["video_path"],
        audio_path=row["audio_path"],
        thumbnail_path=row["thumbnail_path"],
        transcript_status=str(row["transcript_status"]),
        summary_status=str(row["summary_status"]),
        status=str(row["status"]),
    )


class RecordingStore:
    """CRUD dla tabeli ``recordings`` + synchronizacja materiałów (metadata.json ↔ SQLite)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # ── Szybki wpis nagrania (ścieżka rekordera) ──────────────────────────────

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

    # ── Synchronizacja materiału (metadata.json ↔ SQLite) ─────────────────────

    def upsert_material(self, folder: Path, meta: MaterialMetadata) -> int:
        """Wstawia/aktualizuje wiersz materiału wg ``folder`` + synchronizuje tagi.

        ``folder`` (katalog materiału z ``metadata.json``) jest kluczem tożsamości —
        ponowny zapis tych samych metadanych aktualizuje wiersz, nie duplikuje go.
        Zwraca id wiersza.
        """
        folder_str = str(folder)
        conn = connect(self.path)
        try:
            existing = conn.execute(
                "SELECT id FROM recordings WHERE folder = ?", (folder_str,)
            ).fetchone()
            values = (
                meta.title,
                meta.source_type,
                meta.source_url,
                meta.presenter,
                meta.organizer,
                meta.category,
                meta.created_at or _now(),
                meta.duration,
                folder_str,
                meta.video_path,
                meta.audio_path,
                meta.thumbnail_path,
                meta.transcript_status,
                meta.summary_status,
                meta.status,
            )
            if existing is not None:
                rec_id = int(existing["id"])
                conn.execute(
                    "UPDATE recordings SET title=?, source_type=?, source_url=?, presenter=?, "
                    "organizer=?, category=?, created_at=?, duration=?, folder=?, video_path=?, "
                    "audio_path=?, thumbnail_path=?, transcript_status=?, summary_status=?, "
                    "status=? WHERE id=?",
                    (*values, rec_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO recordings (title, source_type, source_url, presenter, organizer, "
                    "category, created_at, duration, folder, video_path, audio_path, "
                    "thumbnail_path, transcript_status, summary_status, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                rec_id = int(cur.lastrowid or 0)
            _write_tags(conn, rec_id, meta.tags)
            conn.commit()
            return rec_id
        finally:
            conn.close()

    def to_metadata(self, recording_id: int) -> MaterialMetadata | None:
        """Czyta wiersz materiału z SQLite z powrotem do :class:`MaterialMetadata`."""
        conn = connect(self.path)
        try:
            row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if row is None:
                return None
            return _row_to_metadata(row, _read_tags(conn, recording_id))
        finally:
            conn.close()

    def list_materials(
        self, *, tag: str | None = None, category: str | None = None
    ) -> list[tuple[int, Path, MaterialMetadata]]:
        """Materiały (id, folder, metadane) z opcjonalnym filtrem po tagu/kategorii.

        Zwraca tylko materiały z folderem (``metadata.json`` jako źródłem prawdy);
        najnowsze pierwsze. Folder jest potrzebny GUI do edycji/zapisu metadanych.
        """
        conn = connect(self.path)
        try:
            join = ""
            clauses = ["r.folder IS NOT NULL"]
            params: list[object] = []
            if tag is not None:
                join = " JOIN tags t ON t.recording_id = r.id AND t.tag = ?"
                params.append(tag)
            if category is not None:
                clauses.append("r.category = ?")
                params.append(category)
            query = (
                f"SELECT DISTINCT r.* FROM recordings r{join} "
                f"WHERE {' AND '.join(clauses)} ORDER BY r.id DESC"
            )
            rows = conn.execute(query, params).fetchall()
            return [
                (
                    int(r["id"]),
                    Path(str(r["folder"])),
                    _row_to_metadata(r, _read_tags(conn, int(r["id"]))),
                )
                for r in rows
            ]
        finally:
            conn.close()

    def all_tags(self) -> list[str]:
        """Wszystkie tagi w bibliotece (unikalne, posortowane) — do filtra."""
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
            return [str(r["tag"]) for r in rows]
        finally:
            conn.close()

    def all_categories(self) -> list[str]:
        """Wszystkie kategorie w bibliotece (unikalne, posortowane) — do filtra."""
        conn = connect(self.path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT category FROM recordings "
                "WHERE category IS NOT NULL AND category <> '' ORDER BY category"
            ).fetchall()
            return [str(r["category"]) for r in rows]
        finally:
            conn.close()
