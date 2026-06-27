"""Schemat SQLite biblioteki + lekkie migracje przez ``PRAGMA user_version``.

Bez ORM-a i bez zależności — czysty ``sqlite3``. Migracje to lista kroków
indeksowanych wersją: przy otwarciu bazy doganiamy ``user_version`` do
:data:`SCHEMA_VERSION`, uruchamiając brakujące kroki w transakcji. Tabela
``jobs`` żyje w tym samym pliku co reszta (kolejka zadań operuje na niej przez
:mod:`core.jobs.store`).

Połączenia są krótkożyciowe i bezpieczne wątkowo (każde wołanie otwiera własne
``sqlite3.Connection``) — pula wątków kolejki nie współdzieli kursora.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: Path) -> sqlite3.Connection:
    """Otwiera połączenie SQLite z sensownymi PRAGMA i ``Row`` jako wierszem.

    ``foreign_keys`` włączone (kaskady), ``WAL`` dla współbieżnych odczytów przy
    pracującej kolejce. Połączenie jest krótkożyciowe — woła je każdy store.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Kontekst transakcji: commit na sukcesie, rollback na wyjątku."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Krok migracji v1 — pełny schemat startowy (recordings/jobs/transcripts/
# summaries/tags/source_profiles). Preferencje skalarne (motyw, katalogi, profil
# obliczeniowy, rejestr dostawców) trzyma config.json (core/config.py), nie SQLite.
_MIGRATION_V1 = """
CREATE TABLE recordings (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    source_url    TEXT,
    category      TEXT,
    created_at    TEXT NOT NULL,
    duration      REAL,
    video_path    TEXT,
    audio_path    TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    checksum      TEXT,
    legal_note    TEXT
);

CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY,
    recording_id  INTEGER REFERENCES recordings(id) ON DELETE CASCADE,
    job_type      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    progress      REAL NOT NULL DEFAULT 0.0,
    error_message TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    max_retries   INTEGER NOT NULL DEFAULT 3,
    payload       TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE transcripts (
    id            INTEGER PRIMARY KEY,
    recording_id  INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    language      TEXT,
    model         TEXT,
    text          TEXT,
    segments_json TEXT
);

CREATE TABLE summaries (
    id            INTEGER PRIMARY KEY,
    recording_id  INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    summary_type  TEXT NOT NULL,
    model         TEXT,
    content       TEXT
);

CREATE TABLE tags (
    id            INTEGER PRIMARY KEY,
    recording_id  INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    tag           TEXT NOT NULL
);
CREATE INDEX idx_tags_tag ON tags(tag);

CREATE TABLE source_profiles (
    id              INTEGER PRIMARY KEY,
    domain          TEXT NOT NULL UNIQUE,
    engine          TEXT,
    auth_method     TEXT,
    quality_preset  TEXT,
    category        TEXT,
    tags            TEXT,
    naming_template TEXT,
    note_mode       TEXT,
    language        TEXT
);
"""

# Migracje indeksowane DOCELOWĄ wersją (po zastosowaniu kroku N user_version=N).
_MIGRATIONS: dict[int, str] = {
    1: _MIGRATION_V1,
}


class Database:
    """Lekki uchwyt bazy biblioteki: tworzy plik i doprowadza schemat do wersji.

    Args:
        path: ścieżka pliku ``library.sqlite3`` (tworzona przy pierwszym otwarciu).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def migrate(self) -> int:
        """Doprowadza schemat do :data:`SCHEMA_VERSION`; zwraca wersję po migracji.

        Idempotentne — uruchamia tylko kroki nowsze niż bieżący ``user_version``.
        """
        conn = connect(self.path)
        try:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for version in range(current + 1, SCHEMA_VERSION + 1):
                step = _MIGRATIONS.get(version)
                if step is None:
                    continue
                with _transaction(conn):
                    conn.executescript(step)
                    # PRAGMA nie przyjmuje parametrów — wersja jest int z zakresu kodu.
                    conn.execute(f"PRAGMA user_version = {version}")
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    def version(self) -> int:
        """Zwraca bieżącą wersję schematu (``user_version``)."""
        conn = connect(self.path)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
