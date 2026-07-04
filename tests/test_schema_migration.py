"""Samonaprawa schematu: stara baza dostaje brakujące kolumny (ALTER ADD COLUMN)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mediaforge.core.library.db import Database, ensure_schema
from mediaforge.core.library.recordings import RecordingStore


def _columns(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    finally:
        conn.close()


def test_old_db_self_heals(tmp_path: Path) -> None:
    """Baza sprzed S2 (bez folder/presenter/…) samonaprawia się przy otwarciu store."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    # Stara tabela BEZ kolumn z S2 (minimalna — chodzi o brak folder/presenter/…).
    conn.execute("CREATE TABLE recordings (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO recordings (name) VALUES ('stary materiał')")
    conn.commit()
    conn.close()

    store = RecordingStore(db)  # ensure_schema biegnie w __init__
    # Regresja: bez samonaprawy → sqlite3.OperationalError: no such column: r.folder
    assert store.list_materials() == []

    cols = _columns(db)
    assert {"folder", "presenter", "organizer", "thumbnail_path"} <= cols  # dorobione

    # Stary wiersz przeżył migrację (dane nieusunięte). list_materials go nie pokazuje,
    # bo folder=NULL (wiersz sprzed metadata.json nie jest „materiałem" w sensie S2),
    # ale ALTER ADD COLUMN zachowuje dane — nie drop+recreate.
    conn = sqlite3.connect(db)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM recordings")]
    finally:
        conn.close()
    assert names == ["stary materiał"]

    # Idempotencja: drugie otwarcie nie rzuca i nie zmienia kolumn.
    RecordingStore(db).list_materials()
    assert _columns(db) == cols


def test_cloud_ok_added_to_old_db_defaults_zero(tmp_path: Path) -> None:
    """cloud_ok dorabiane na starej bazie; istniejący wiersz dostaje 0 (fail-safe: lokalnie)."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recordings (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO recordings (title) VALUES ('sprzed S4')")
    conn.commit()
    conn.close()

    RecordingStore(db)  # ensure_schema dorabia cloud_ok INTEGER DEFAULT 0
    assert {"cloud_ok", "summary_path"} <= _columns(db)

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT cloud_ok FROM recordings WHERE title = 'sprzed S4'").fetchone()
    finally:
        conn.close()
    assert row[0] == 0  # stary wiersz jest wrażliwy (lokalnie), nie 1


def _profile_columns(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(source_profiles)")}
    finally:
        conn.close()


def test_old_source_profiles_gets_new_columns(tmp_path: Path) -> None:
    """Stara tabela source_profiles (sprzed S5) dostaje organizer/cloud_ok; wiersz → cloud_ok=0."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    # Kształt source_profiles sprzed S5 (bez organizer/cloud_ok).
    conn.execute(
        "CREATE TABLE source_profiles (id INTEGER PRIMARY KEY, domain TEXT NOT NULL UNIQUE, "
        "category TEXT, tags TEXT)"
    )
    conn.execute("INSERT INTO source_profiles (domain, category) VALUES ('a.example.com', 'K')")
    conn.commit()
    conn.close()

    ensure_schema(db)
    assert {"organizer", "cloud_ok"} <= _profile_columns(db)
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT cloud_ok FROM source_profiles WHERE domain = 'a.example.com'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 0  # stary profil jest lokalny (fail-safe), nie 1


def test_fresh_db_ensure_schema_is_noop(tmp_path: Path) -> None:
    """Świeża baza (pełny schemat) → ponowne ensure_schema nic nie dodaje (brak ALTER)."""
    db = tmp_path / "fresh.sqlite3"
    Database(db).migrate()
    before = _columns(db)
    assert {"folder", "presenter", "organizer", "thumbnail_path", "status"} <= before

    ensure_schema(db)  # ponowne — no-op
    assert _columns(db) == before


def test_migrate_stamps_user_version(tmp_path: Path) -> None:
    """Po samonaprawie user_version jest ostemplowany (zaczep na migracje nie-addytywne)."""
    db = Database(tmp_path / "v.sqlite3")
    from mediaforge.core.library.db import SCHEMA_VERSION

    assert db.migrate() == SCHEMA_VERSION
    assert db.version() == SCHEMA_VERSION
