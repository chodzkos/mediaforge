"""Schemat SQLite biblioteki + **samonaprawiająca się** migracja addytywna.

Bez ORM-a i bez zależności — czysty ``sqlite3``. :func:`ensure_schema` jest idempotentne
i NIE-destrukcyjne: tworzy brakujące tabele (``CREATE TABLE IF NOT EXISTS``) i **dorabia
brakujące kolumny** istniejącym tabelom (``ALTER TABLE ADD COLUMN``). Dzięki temu baza
sprzed dołożenia kolumny samonaprawia się przy starcie, bez ręcznego kasowania pliku.

Świadomie ALTER ADD COLUMN, NIE drop+recreate: gdyby baza biblioteki była na NAS-ie
(QNAP/Tailscale) chwilowo offline, drop+rescan dałby pustą bibliotekę. ALTER zachowuje
istniejące dane i indeks i nie zależy od dostępności katalogu materiałów.

Połączenia są krótkożyciowe i bezpieczne wątkowo (każde wołanie otwiera własne
``sqlite3.Connection``) — pula wątków kolejki nie współdzieli kursora.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# Jedno źródło prawdy dla kolumn tabeli ``recordings`` (nazwa → deklaracja SQL).
# Używane ZARÓWNO w CREATE TABLE, JAK i w ensure_schema (ALTER ADD COLUMN) — brak ryzyka
# rozjazdu dwóch list. UWAGA (limit SQLite ALTER ADD COLUMN): kolumny dorabiane do
# istniejącej tabeli muszą być nullable albo mieć STAŁY default — PRIMARY KEY oraz
# NOT NULL bez defaultu są pomijane przez _ensure_columns (istnieją od początku w CREATE).
_RECORDINGS_COLUMNS: dict[str, str] = {
    "id": "INTEGER PRIMARY KEY",
    "title": "TEXT NOT NULL",
    "source_type": "TEXT NOT NULL",
    "source_url": "TEXT",
    "presenter": "TEXT",
    "organizer": "TEXT",
    "category": "TEXT",
    "created_at": "TEXT NOT NULL",
    "duration": "REAL",
    "folder": "TEXT",
    "video_path": "TEXT",
    "audio_path": "TEXT",
    "thumbnail_path": "TEXT",
    "transcript_status": "TEXT NOT NULL DEFAULT 'none'",
    "transcript_json": "TEXT",
    "transcript_srt": "TEXT",
    "summary_status": "TEXT NOT NULL DEFAULT 'none'",
    "status": "TEXT NOT NULL DEFAULT 'new'",
    "checksum": "TEXT",
    "legal_note": "TEXT",
}

# Pozostałe tabele (na razie addytywnie nie ewoluują — gdy zaczną, mają własne dicty kolumn
# i wpis w _ensure_columns, analogicznie do recordings).
_OTHER_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        id            INTEGER PRIMARY KEY,
        recording_id  INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        language      TEXT,
        model         TEXT,
        text          TEXT,
        segments_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS summaries (
        id            INTEGER PRIMARY KEY,
        recording_id  INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        summary_type  TEXT NOT NULL,
        model         TEXT,
        content       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id            INTEGER PRIMARY KEY,
        recording_id  INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        tag           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)",
    """
    CREATE TABLE IF NOT EXISTS source_profiles (
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
    )
    """,
)


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


def _columns_sql(columns: dict[str, str]) -> str:
    """Składa fragment kolumn do ``CREATE TABLE`` z dicta {nazwa: deklaracja}."""
    return ",\n    ".join(f"{name} {decl}" for name, decl in columns.items())


def _alter_safe(decl: str) -> bool:
    """Czy kolumnę da się dorobić przez ``ALTER ADD COLUMN`` (limit SQLite).

    Nie wolno: PRIMARY KEY ani NOT NULL bez stałego defaultu — takie kolumny istnieją od
    początku w ``CREATE TABLE`` i na realnej starej bazie i tak już są.
    """
    upper = decl.upper()
    if "PRIMARY KEY" in upper:
        return False
    return not ("NOT NULL" in upper and "DEFAULT" not in upper)


def _ensure_columns(conn: sqlite3.Connection, table: str, expected: dict[str, str]) -> None:
    """Dodaje brakujące kolumny do istniejącej tabeli. Idempotentne, nie-destrukcyjne.

    ``PRAGMA table_info`` zwraca krotki ``(cid, name, type, notnull, dflt, pk)`` — nazwa
    jest pod indeksem 1. Kolumny nie do dorobienia (PK / NOT NULL bez defaultu) są pomijane.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in expected.items():
        if name not in existing and _alter_safe(decl):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def ensure_schema(path: Path) -> None:
    """Tworzy brakujące tabele i dorabia brakujące kolumny (samonaprawa starych baz).

    Idempotentne i nie-destrukcyjne: na świeżej bazie tworzy pełny schemat, na starej
    (sprzed dołożenia kolumny) wykonuje ``ALTER ADD COLUMN`` zachowując dane i indeks.
    Stempluje ``PRAGMA user_version`` — to zaczep na przyszłe migracje NIE-addytywne
    (rename/zmiana typu/drop, których ALTER ADD COLUMN nie ogarnia; idą tu, kluczowane
    po ``user_version``). Sam diff kolumn pokrywa wszystkie przypadki addytywne.
    """
    columns = _columns_sql(_RECORDINGS_COLUMNS)
    conn = connect(path)
    try:
        with _transaction(conn):
            conn.execute(f"CREATE TABLE IF NOT EXISTS recordings (\n    {columns}\n)")
            for statement in _OTHER_SCHEMA:
                conn.execute(statement)
            _ensure_columns(conn, "recordings", _RECORDINGS_COLUMNS)
            # PRAGMA nie przyjmuje parametrów — wersja jest int z zakresu kodu.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


class Database:
    """Lekki uchwyt bazy biblioteki: tworzy plik i doprowadza schemat do wersji.

    Args:
        path: ścieżka pliku ``library.sqlite3`` (tworzona przy pierwszym otwarciu).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def migrate(self) -> int:
        """Doprowadza schemat do :data:`SCHEMA_VERSION` (samonaprawa); zwraca wersję."""
        ensure_schema(self.path)
        return self.version()

    def version(self) -> int:
        """Zwraca bieżącą wersję schematu (``user_version``)."""
        conn = connect(self.path)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
