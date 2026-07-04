"""Profile źródeł: round-trip, domyślne metadane pobrania, nienadpisywanie edycji materiału."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from mediaforge.core.engines.download_engine import (
    DownloaderEngine,
    LineCb,
    RunResult,
    domain_of,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import write_metadata
from mediaforge.core.library.profiles import SourceProfile, SourceProfileStore
from mediaforge.core.library.recordings import RecordingStore

_URL = "https://www.konferencja.example.com/wyklad/abc"


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    Database(db).migrate()
    return db


def test_profile_round_trip(tmp_path: Path) -> None:
    store = SourceProfileStore(_db(tmp_path))
    profile = SourceProfile(
        domain="konferencja.example.com",
        category="Konferencje",
        tags=("kardiologia", "2026"),
        organizer="PTK",
        cloud_ok=True,
    )
    store.upsert(profile)
    assert store.get("konferencja.example.com") == profile
    # Idempotentny upsert po domenie (aktualizuje, nie duplikuje).
    store.upsert(replace(profile, category="Wykłady"))
    updated = store.get("konferencja.example.com")
    assert updated is not None and updated.category == "Wykłady"
    assert len(store.list_all()) == 1


def test_unknown_domain_is_none_and_default_false(tmp_path: Path) -> None:
    """Brak profilu = None (materiał dostanie globalny default cloud_ok=False)."""
    store = SourceProfileStore(_db(tmp_path))
    assert store.get("nieznana.example.com") is None
    store.upsert(SourceProfile(domain="x.example.com"))  # bez cloud_ok
    got = store.get("x.example.com")
    assert got is not None and got.cloud_ok is False


def _fake_runner(command: list[str], on_line: LineCb | None = None) -> RunResult:
    out_dir = Path(command[command.index("-o") + 1]).parent
    (out_dir / "Ep.mp3").write_bytes(b"AUDIO")
    (out_dir / "Ep.info.json").write_text(
        json.dumps({"title": "Ep", "ext": "mp3", "duration": 100}), encoding="utf-8"
    )
    return RunResult(returncode=0, tail="")


def test_download_uses_profile_defaults(tmp_path: Path) -> None:
    """Prefill: profil domeny → domyślne kategoria/tagi/organizator + cloud_ok nowego materiału."""
    db = _db(tmp_path)
    profiles = SourceProfileStore(db)
    profiles.upsert(
        SourceProfile(
            domain=domain_of(_URL),
            category="Konferencje",
            tags=("kardio",),
            organizer="PTK",
            cloud_ok=True,  # użytkownik ŚWIADOMIE zezwolił na chmurę dla tego źródła
        )
    )
    store = RecordingStore(db)
    profile = profiles.get(domain_of(_URL))
    assert profile is not None

    DownloaderEngine(store=store, runner=_fake_runner).download(
        _URL,
        tmp_path / "lib",
        lambda _f, _m: None,
        audio_only=True,
        title="Ep",
        category=profile.category,
        tags=list(profile.tags),
        organizer=profile.organizer,
        cloud_ok=profile.cloud_ok,
    )

    meta = store.list_materials()[0][2]
    assert meta.category == "Konferencje" and meta.tags == ["kardio"]
    assert meta.organizer == "PTK" and meta.cloud_ok is True


def test_profile_not_reapplied_after_material_edit(tmp_path: Path) -> None:
    """Profil daje wartość POCZĄTKOWĄ; późniejsza edycja materiału nie jest nadpisywana."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    DownloaderEngine(store=store, runner=_fake_runner).download(
        _URL, tmp_path / "lib", lambda _f, _m: None, audio_only=True, title="Ep", cloud_ok=True
    )
    rec_id, folder, meta = store.list_materials()[0]
    assert meta.cloud_ok is True

    # Użytkownik cofa zgodę na chmurę dla TEGO materiału (metadata.json = źródło prawdy + indeks).
    edited = replace(meta, cloud_ok=False)
    write_metadata(folder, edited)
    store.upsert_material(folder, edited)
    # Profil nie jest re-aplikowany — zmiana przeżywa (nawet po rescanie z folderów).
    store.rescan(tmp_path / "lib")
    again = store.get_material(rec_id)
    assert again is not None and again[1].cloud_ok is False
