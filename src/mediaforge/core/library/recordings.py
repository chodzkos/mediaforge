"""Magazyn nagrań nad tabelą ``recordings`` (indeks materiałów + statusy).

Czysty ``sqlite3`` (bez Qt), krótkożyciowe połączenia przez
:func:`core.library.db.connect`. Folder materiału z ``metadata.json``
(:mod:`core.library.material`) jest **źródłem prawdy**; ta tabela to indeks nad nim —
:meth:`RecordingStore.upsert_material` synchronizuje metadane do SQLite (lista/filtry),
a :meth:`RecordingStore.to_metadata` czyta je z powrotem (round-trip folder ↔ SQLite).
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from sqlite3 import Connection, Row

from mediaforge.core.library.db import connect, ensure_schema
from mediaforge.core.library.material import (
    MaterialMetadata,
    metadata_path,
    read_metadata,
    write_metadata,
)
from mediaforge.core.library.slides import (
    Slide,
    attach_slides,
    read_slides,
    slide_from_dict,
    slide_to_dict,
)

logger = logging.getLogger("mediaforge")


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


def is_inside_library(path: Path, library_root: Path) -> bool:
    """Czy ``path`` leży w bibliotece (pod ``library_root``) — JEDNA definicja „w bibliotece".

    Wspólna dla path-safety usuwania (:meth:`RecordingStore.delete_material`) i ostrzeżenia
    GUI o zapisie poza rootem. ``resolve()`` rozwija symlinki/``..``, więc porównanie jest
    realne (nie leksykalne).
    """
    return path.resolve().is_relative_to(library_root.resolve())


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
        transcript_json=row["transcript_json"],
        transcript_srt=row["transcript_srt"],
        summary_status=str(row["summary_status"]),
        summary_path=row["summary_path"],
        summary_parts_path=row["summary_parts_path"],
        notes_status=str(row["notes_status"]),
        notes_path=row["notes_path"],
        cloud_ok=bool(row["cloud_ok"]),  # INTEGER 0/1 → bool (fail-safe: brak/0 = lokalnie)
        slides=_slides_from_json(row["slides_json"]),
        status=str(row["status"]),
    )


def _slides_from_json(raw: str | None) -> tuple[Slide, ...]:
    """Deserializuje kolumnę ``slides_json`` (indeks nad ``slides/``) do krotki :class:`Slide`."""
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    return tuple(slide_from_dict(s) for s in data if isinstance(s, dict))


class RecordingStore:
    """CRUD dla tabeli ``recordings`` + synchronizacja materiałów (metadata.json ↔ SQLite)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # Samonaprawa schematu przy starcie: stara baza (sprzed dołożenia kolumny) dostaje
        # brakujące kolumny (ALTER ADD COLUMN), więc list_materials nie rzuci „no such column".
        ensure_schema(path)

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

    def delete_material(self, recording_id: int, library_root: Path) -> None:
        """Usuwa materiał: NAJPIERW folder (``rmtree``), POTEM wiersz (FK kaskada sprząta joby).

        Kolejność folder→wiersz = spójność: gdy ``rmtree`` padnie (plik otwarty w odtwarzaczu,
        NAS offline), wyjątek propaguje, a wpis ZOSTAJE — baza nie twierdzi, że usunęła coś,
        co leży na dysku. Odmawia (``ValueError``, nic nie ruszając), gdy:

        - materiał ma aktywny job (``pending``/``running``) — ``rmtree`` spod działającego
          whisper-cli = crash handlera w połowie zapisu;
        - folder jest poza ``library_root`` (path-safety — nigdy nie ``rmtree`` spoza
          biblioteki, nawet przy uszkodzonym wpisie ``folder``).
        """
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT folder FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
            if row is None or row["folder"] is None:
                raise ValueError(f"Materiał #{recording_id} nie istnieje")
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE recording_id = ? "
                "AND status IN ('pending', 'running')",
                (recording_id,),
            ).fetchone()
            if active["n"]:
                raise ValueError("Materiał ma aktywne zadanie (transkrypcja w toku)")
            folder = Path(str(row["folder"]))
            if not is_inside_library(folder, library_root):
                raise ValueError(
                    f"Materiał leży poza biblioteką ({folder}) — aplikacja nim nie zarządza; "
                    "usuń folder ręcznie."
                )
            if folder.exists():
                shutil.rmtree(folder)  # ignore_errors=False → błąd widoczny, wpis zostaje
            conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
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
                meta.transcript_json,
                meta.transcript_srt,
                meta.summary_status,
                meta.summary_path,
                meta.summary_parts_path,
                meta.notes_status,
                meta.notes_path,
                int(meta.cloud_ok),  # bool → INTEGER 0/1
                json.dumps([slide_to_dict(s) for s in meta.slides]),
                meta.status,
            )
            if existing is not None:
                rec_id = int(existing["id"])
                conn.execute(
                    "UPDATE recordings SET title=?, source_type=?, source_url=?, presenter=?, "
                    "organizer=?, category=?, created_at=?, duration=?, folder=?, video_path=?, "
                    "audio_path=?, thumbnail_path=?, transcript_status=?, transcript_json=?, "
                    "transcript_srt=?, summary_status=?, summary_path=?, summary_parts_path=?, "
                    "notes_status=?, notes_path=?, cloud_ok=?, slides_json=?, status=? WHERE id=?",
                    (*values, rec_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO recordings (title, source_type, source_url, presenter, organizer, "
                    "category, created_at, duration, folder, video_path, audio_path, "
                    "thumbnail_path, transcript_status, transcript_json, transcript_srt, "
                    "summary_status, summary_path, summary_parts_path, notes_status, notes_path, "
                    "cloud_ok, slides_json, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                rec_id = int(cur.lastrowid or 0)
            _write_tags(conn, rec_id, meta.tags)
            conn.commit()
            return rec_id
        finally:
            conn.close()

    def add_slides(
        self, folder: Path, meta: MaterialMetadata, sources: list[Path]
    ) -> MaterialMetadata:
        """Podłącza slajdy: kopia obrazów do ``slides/`` → metadata.json (źródło prawdy) + indeks.

        Kopiuje tylko obrazy (nie-obrazy pomija), przelicza kolejność/timestampy
        (:func:`~mediaforge.core.library.slides.collect_slides`) i zwraca zaktualizowane metadane.
        """
        collected = attach_slides(folder, sources)
        updated = replace(meta, slides=tuple(collected))
        write_metadata(folder, updated)
        self.upsert_material(folder, updated)
        return updated

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

    def get_material(self, recording_id: int) -> tuple[Path, MaterialMetadata] | None:
        """Zwraca (folder, metadane) materiału po id — dla joba transkrypcji/edycji."""
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT * FROM recordings WHERE id = ? AND folder IS NOT NULL", (recording_id,)
            ).fetchone()
            if row is None:
                return None
            folder = Path(str(row["folder"]))
            return folder, _row_to_metadata(row, _read_tags(conn, recording_id))
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

    def rescan(self, library_root: Path, *, prune: bool = True) -> int:
        """Odbudowuje/synchronizuje indeks SQLite z ``metadata.json`` w podfolderach biblioteki.

        Czyni „folder = źródło prawdy" realnym: indeks da się odtworzyć z dysku po skasowaniu
        bazy, po przeniesieniu biblioteki na inną maszynę albo po ręcznej edycji ``metadata.json``.
        Iteruje podkatalogi ``library_root``, dla każdego z ``metadata.json`` robi upsert.
        Gdy ``prune`` — usuwa z indeksu materiały, których folder zniknął z dysku.
        Zwraca liczbę zindeksowanych materiałów.

        Bezpieczeństwo prune (biblioteka na NAS-ie/sieci): NIE prunuje, gdy ``library_root``
        nie da się wylistować (offline/brak dostępu) ANI gdy skan wyszedł pusty przy niepustym
        indeksie — to sygnał „root niedostępny", nie „wszystko usunięte". Inaczej jeden klik
        przy chwilowo nieosiągalnym NAS-ie wymazałby cały indeks.
        """
        try:
            children = sorted(library_root.iterdir())
            listed = True
        except OSError:
            children, listed = [], False  # root niedostępny (np. NAS offline) — nie listujemy
        count = 0
        for child in children:
            if not (child.is_dir() and metadata_path(child).is_file()):
                continue
            try:
                meta = read_metadata(child)
            except (OSError, ValueError) as exc:
                # Uszkodzony/nieczytelny metadata.json — raportujemy i pomijamy TEN materiał,
                # nie wywalamy całego skanu (jeden zły folder nie kasuje indeksu reszty).
                logger.warning("Nieczytelny metadata.json — pomijam materiał: %s (%s)", child, exc)
                continue
            # Slajdy odtwarzamy z folderu ``slides/`` (źródło prawdy = dysk) — dołożony ręcznie
            # plik jest podłapywany; przy zmianie zapisujemy metadata.json (jak transkrypt).
            disk_slides = tuple(read_slides(child))
            if disk_slides != meta.slides:
                meta = replace(meta, slides=disk_slides)
                write_metadata(child, meta)
            self.upsert_material(child, meta)
            count += 1
        suspicious_empty = count == 0 and self._material_count() > 0
        if prune and listed and not suspicious_empty:
            self._prune_missing_folders()
        return count

    def _material_count(self) -> int:
        """Liczba materiałów w indeksie (wierszy z folderem) — guard prune."""
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM recordings WHERE folder IS NOT NULL"
            ).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def _prune_missing_folders(self) -> None:
        """Usuwa z indeksu materiały, których folder zniknął z dysku (kaskada na tagi itd.)."""
        conn = connect(self.path)
        try:
            rows = conn.execute(
                "SELECT id, folder FROM recordings WHERE folder IS NOT NULL"
            ).fetchall()
            for row in rows:
                folder = str(row["folder"])
                if folder and not Path(folder).exists():
                    conn.execute("DELETE FROM recordings WHERE id = ?", (int(row["id"]),))
            conn.commit()
        finally:
            conn.close()
