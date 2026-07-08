"""Round-trip metadanych materiału: MaterialMetadata ↔ metadata.json ↔ SQLite."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

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


def test_cloud_ok_missing_field_defaults_false(tmp_path: Path) -> None:
    """BRAK pola cloud_ok w metadata.json = False (fail-safe: zapomnienie = lokalnie)."""
    # Metadane bez cloud_ok (np. sprzed S4) — from_dict musi dać False, nie rzucić.
    assert MaterialMetadata.from_dict({"title": "X", "created_at": "t"}).cloud_ok is False
    # Zapis pełnych metadanych domyślnie też ma cloud_ok=False.
    assert _meta().cloud_ok is False


def test_cloud_ok_and_summary_path_round_trip(tmp_path: Path) -> None:
    """cloud_ok=True i summary_path przeżywają round-trip metadata.json ↔ SQLite."""
    meta = replace(_meta(), cloud_ok=True, summary_status="done", summary_path="summary.md")
    material_dir = tmp_path / "material"
    write_metadata(material_dir, meta)
    assert read_metadata(material_dir) == meta  # JSON round-trip

    store = _store(tmp_path)
    rec_id = store.upsert_material(material_dir, read_metadata(material_dir))
    from_sql = store.to_metadata(rec_id)
    assert from_sql is not None
    assert from_sql.cloud_ok is True and from_sql.summary_path == "summary.md"
    assert from_sql == meta  # SQLite round-trip zachowuje wszystkie pola


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


# ── Atomowy zapis + odporność na uszkodzony metadata.json ─────────────────────


def test_leftover_tmp_does_not_break_read(tmp_path: Path) -> None:
    """Śmieciowy plik tymczasowy po przerwanym zapisie nie psuje odczytu metadata.json."""
    material_dir = tmp_path / "material"
    write_metadata(material_dir, _meta())
    # Symulacja niedokończonego atomowego zapisu: obcięty tmp obok metadata.json.
    (material_dir / ".metadata-leftover.json.tmp").write_text("{ niedoko", encoding="utf-8")
    assert read_metadata(material_dir) == _meta()  # czytamy WYŁĄCZNIE metadata.json


def test_write_metadata_leaves_no_tmp_on_success(tmp_path: Path) -> None:
    """Po udanym (atomowym) zapisie w folderze nie zostaje żaden plik ``.tmp``."""
    material_dir = tmp_path / "material"
    write_metadata(material_dir, _meta())
    assert [p.name for p in material_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_read_metadata_corrupt_json_raises_valueerror_with_path(tmp_path: Path) -> None:
    """Uszkodzony metadata.json → czytelny ValueError ze ścieżką (skan łapie per-materiał)."""
    material_dir = tmp_path / "material"
    material_dir.mkdir()
    (material_dir / "metadata.json").write_text("{ to nie jest JSON", encoding="utf-8")
    with pytest.raises(ValueError, match=r"Uszkodzony metadata\.json") as exc_info:
        read_metadata(material_dir)
    assert str(material_dir) in str(exc_info.value)  # ścieżka w komunikacie


def test_rescan_skips_corrupt_material(tmp_path: Path) -> None:
    """Uszkodzony metadata.json nie wywraca skanu — pozostałe materiały są indeksowane."""
    lib = tmp_path / "lib"
    write_metadata(lib / "Dobry", MaterialMetadata(title="Dobry", created_at="t"))
    bad = lib / "Zly"
    bad.mkdir(parents=True)
    (bad / "metadata.json").write_text("{ uszkodzony", encoding="utf-8")

    store = _store(tmp_path)
    indexed = store.rescan(lib)

    assert indexed == 1  # tylko dobry materiał; zły pominięty, skan nie padł
    assert [m.title for _, _f, m in store.list_materials()] == ["Dobry"]


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
