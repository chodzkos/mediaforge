"""Schemat SQLite biblioteki + migracje przez ``user_version``."""

from __future__ import annotations

from pathlib import Path

from mediaforge.core.library import SCHEMA_VERSION, Database, connect

_EXPECTED_TABLES = {
    "recordings",
    "jobs",
    "transcripts",
    "summaries",
    "tags",
    "settings",
    "source_profiles",
}


def test_migrate_creates_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    assert db.migrate() == SCHEMA_VERSION
    assert db.version() == SCHEMA_VERSION

    conn = connect(db.path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    tables = {r["name"] for r in rows}
    assert tables >= _EXPECTED_TABLES


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    assert db.migrate() == SCHEMA_VERSION
    # Druga migracja nie powinna nic zmienić ani rzucić.
    assert db.migrate() == SCHEMA_VERSION
