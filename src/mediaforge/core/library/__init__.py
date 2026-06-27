"""Biblioteka mediaforge — magazyn SQLite nad folderami materiałów.

SQLite jest indeksem/cache nad układem „jeden materiał = jeden folder"; folder
zostaje przenośny (``metadata.json`` to źródło prawdy obok bazy). Tu mieszka
schemat i lekkie migracje (``PRAGMA user_version``) — patrz :mod:`.db`.
"""

from __future__ import annotations

from mediaforge.core.library.db import (
    SCHEMA_VERSION,
    Database,
    connect,
)

__all__ = ["SCHEMA_VERSION", "Database", "connect"]
