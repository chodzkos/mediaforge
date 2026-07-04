"""Profile źródeł (per domena) — domyślne metadane materiałów z danego źródła.

Spłata długu z S2 (tabela ``source_profiles`` czekała w schemacie). Profil per domena URL
niesie DOMYŚLNE kategorię/tagi/organizatora + ``cloud_ok`` dla NOWYCH materiałów z tego
źródła. Fail-safe **nienaruszony**: globalny default ``cloud_ok`` pozostaje ``False``; profil
może go podnieść TYLKO dlatego, że użytkownik świadomie tak ustawił dla tego źródła. Profil
daje wartości POCZĄTKOWE materiału przy pobraniu — późniejsze edycje w bibliotece nie są
nadpisywane (profil nie jest re-aplikowany do istniejących materiałów).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row

from mediaforge.core.library.db import connect, ensure_schema


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Domyślne metadane dla domeny źródła (klucz: ``domain``)."""

    domain: str
    category: str | None = None
    tags: tuple[str, ...] = ()
    organizer: str | None = None
    cloud_ok: bool = False  # DOMYŚLNY cloud_ok nowego materiału (fail-safe: brak profilu = False)


def _row_to_profile(row: Row) -> SourceProfile:
    raw_tags = row["tags"]
    try:
        tags = tuple(str(t) for t in json.loads(raw_tags)) if raw_tags else ()
    except (ValueError, TypeError):
        tags = ()
    return SourceProfile(
        domain=str(row["domain"]),
        category=row["category"],
        tags=tags,
        organizer=row["organizer"],
        cloud_ok=bool(row["cloud_ok"]),
    )


class SourceProfileStore:
    """CRUD dla tabeli ``source_profiles`` (profil per domena; tagi jako JSON w kolumnie TEXT)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        ensure_schema(path)

    def upsert(self, profile: SourceProfile) -> None:
        """Wstawia/aktualizuje profil po domenie (UPSERT — domena jest kluczem UNIQUE)."""
        conn = connect(self.path)
        try:
            conn.execute(
                "INSERT INTO source_profiles (domain, category, tags, organizer, cloud_ok) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET category=excluded.category, "
                "tags=excluded.tags, organizer=excluded.organizer, cloud_ok=excluded.cloud_ok",
                (
                    profile.domain,
                    profile.category,
                    json.dumps(list(profile.tags)),
                    profile.organizer,
                    int(profile.cloud_ok),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, domain: str) -> SourceProfile | None:
        """Zwraca profil domeny albo ``None`` (brak profilu = globalny default, cloud_ok=False)."""
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT * FROM source_profiles WHERE domain = ?", (domain,)
            ).fetchone()
            return _row_to_profile(row) if row is not None else None
        finally:
            conn.close()

    def list_all(self) -> list[SourceProfile]:
        """Wszystkie profile (po domenie) — do edytora profili w GUI."""
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT * FROM source_profiles ORDER BY domain").fetchall()
            return [_row_to_profile(r) for r in rows]
        finally:
            conn.close()

    def delete(self, domain: str) -> None:
        """Usuwa profil domeny (idempotentnie)."""
        conn = connect(self.path)
        try:
            conn.execute("DELETE FROM source_profiles WHERE domain = ?", (domain,))
            conn.commit()
        finally:
            conn.close()
