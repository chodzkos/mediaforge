"""Round-trip metadanych materiału: MaterialMetadata ↔ metadata.json ↔ SQLite."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mediaforge.core.library.db import Database
from mediaforge.core.library.material import (
    MaterialMetadata,
    read_metadata,
    write_metadata,
)
from mediaforge.core.library.recordings import RecordingStore


def _meta() -> MaterialMetadata:
    return MaterialMetadata(
        title="Wykład o sieciach",
        created_at="2026-06-30T10:00:00+00:00",
        source_type="import",
        source_url=None,
        presenter="dr Jan Kowalski",
        organizer="Politechnika",
        category="Sieci",
        tags=["tcp", "routing", "bgp"],
        duration=3601.5,
        video_path="wyklad.mp4",
        audio_path="wyklad.m4a",
        thumbnail_path="thumbnail.jpg",
        transcript_status="none",
        summary_status="none",
        status="recorded",
    )


def _store(tmp_path: Path) -> RecordingStore:
    db_path = tmp_path / "library.sqlite3"
    Database(db_path).migrate()
    return RecordingStore(db_path)


def test_metadata_json_round_trip(tmp_path: Path) -> None:
    """metadata.json zapisany i odczytany zwraca identyczne metadane."""
    meta = _meta()
    write_metadata(tmp_path, meta)
    assert read_metadata(tmp_path) == meta


def test_metadata_folder_to_sqlite_round_trip(tmp_path: Path) -> None:
    """Folder (metadata.json) → SQLite → odczyt z SQLite zwraca te same metadane."""
    meta = _meta()
    material_dir = tmp_path / "material"
    write_metadata(material_dir, meta)
    store = _store(tmp_path)

    rec_id = store.upsert_material(material_dir, read_metadata(material_dir))
    from_sql = store.to_metadata(rec_id)

    assert from_sql == meta
    # Spójność obu źródeł.
    assert from_sql == read_metadata(material_dir)


def test_upsert_is_idempotent_by_folder(tmp_path: Path) -> None:
    """Ponowny zapis tych samych metadanych aktualizuje wiersz, nie duplikuje."""
    meta = _meta()
    material_dir = tmp_path / "material"
    write_metadata(material_dir, meta)
    store = _store(tmp_path)

    id1 = store.upsert_material(material_dir, meta)
    updated = replace(meta, presenter="prof. Nowak")
    id2 = store.upsert_material(material_dir, updated)

    assert id1 == id2
    assert len(store.list_materials()) == 1
    assert store.list_materials()[0][1] == material_dir  # folder w wyniku
    back = store.to_metadata(id1)
    assert back is not None and back.presenter == "prof. Nowak"


def test_list_filters_by_tag_and_category(tmp_path: Path) -> None:
    """Biblioteka filtruje po tagu i po kategorii."""
    store = _store(tmp_path)
    a = MaterialMetadata(title="A", created_at="t", category="Sieci", tags=["bgp", "tcp"])
    b = MaterialMetadata(title="B", created_at="t", category="AI", tags=["llm"])
    store.upsert_material(tmp_path / "a", a)
    store.upsert_material(tmp_path / "b", b)

    assert {m.title for _, _f, m in store.list_materials()} == {"A", "B"}
    assert [m.title for _, _f, m in store.list_materials(tag="bgp")] == ["A"]
    assert [m.title for _, _f, m in store.list_materials(category="AI")] == ["B"]
    assert store.all_tags() == ["bgp", "llm", "tcp"]
    assert store.all_categories() == ["AI", "Sieci"]


def test_rescan_rebuilds_index_from_folders(tmp_path: Path) -> None:
    """Folder = źródło prawdy: indeks SQLite odbudowywalny z metadata.json (pusta baza)."""
    lib = tmp_path / "lib"
    write_metadata(lib / "A", MaterialMetadata(title="A", created_at="t", tags=["x"]))
    write_metadata(lib / "B", MaterialMetadata(title="B", created_at="t", category="K"))
    # Folder bez metadata.json jest ignorowany.
    (lib / "smieci").mkdir(parents=True)

    store = _store(tmp_path)  # świeża, pusta baza
    indexed = store.rescan(lib)

    assert indexed == 2
    assert {m.title for _, _f, m in store.list_materials()} == {"A", "B"}


def test_rescan_picks_up_manual_edit(tmp_path: Path) -> None:
    """Ręczna edycja metadata.json jest podchwytywana przez rescan (re-sync)."""
    lib = tmp_path / "lib"
    folder = lib / "A"
    write_metadata(folder, MaterialMetadata(title="A", created_at="t"))
    store = _store(tmp_path)
    store.rescan(lib)

    write_metadata(folder, MaterialMetadata(title="A poprawione", created_at="t", tags=["nowy"]))
    store.rescan(lib)

    materials = store.list_materials()
    assert len(materials) == 1
    assert materials[0][2].title == "A poprawione"
    assert materials[0][2].tags == ["nowy"]


def test_rescan_prunes_deleted_folder(tmp_path: Path) -> None:
    """Materiał, którego folder zniknął z dysku, znika z indeksu (prune)."""
    import shutil

    lib = tmp_path / "lib"
    write_metadata(lib / "A", MaterialMetadata(title="A", created_at="t"))
    write_metadata(lib / "B", MaterialMetadata(title="B", created_at="t"))
    store = _store(tmp_path)
    store.rescan(lib)
    assert len(store.list_materials()) == 2

    shutil.rmtree(lib / "B")
    store.rescan(lib)
    assert [m.title for _, _f, m in store.list_materials()] == ["A"]


def test_rescan_does_not_wipe_index_when_root_unavailable(tmp_path: Path) -> None:
    """NAS offline: root niedostępny → prune NIE kasuje indeksu (prawda przeżyje na NAS-ie)."""
    import shutil

    lib = tmp_path / "lib"
    write_metadata(lib / "A", MaterialMetadata(title="A", created_at="t"))
    write_metadata(lib / "B", MaterialMetadata(title="B", created_at="t"))
    store = _store(tmp_path)
    store.rescan(lib)
    assert len(store.list_materials()) == 2

    shutil.rmtree(lib)  # symulacja: cały root znika (NAS offline / ścieżka pusta)
    assert store.rescan(lib) == 0
    # KLUCZOWE: indeks nietknięty — bez tego guardu prune wymazałby wszystko.
    assert len(store.list_materials()) == 2


def test_rescan_skips_prune_on_empty_scan_with_nonempty_index(tmp_path: Path) -> None:
    """Zero znalezionych przy niepustym indeksie = root niedostępny → NIE pruneuj."""
    import shutil

    lib = tmp_path / "lib"
    write_metadata(lib / "A", MaterialMetadata(title="A", created_at="t"))
    store = _store(tmp_path)
    store.rescan(lib)
    shutil.rmtree(lib / "A")  # folder materiału znika (np. odmontowany NAS)

    empty = tmp_path / "empty"  # pusty, ale ISTNIEJĄCY root (np. pusto rozwiązana ścieżka)
    empty.mkdir()
    assert store.rescan(empty) == 0
    assert len(store.list_materials()) == 1  # wpis zachowany, nie wymazany
