"""Parser slajdów (kolejność + timestamp z nazwy) i podłączanie do folderu materiału."""

from __future__ import annotations

from pathlib import Path

from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.core.library.slides import (
    SLIDES_DIRNAME,
    Slide,
    attach_slides,
    collect_slides,
    parse_slide_timestamp,
    read_slides,
)

# ── Parser (5 testów z piaskownicy) ───────────────────────────────────────────


def test_timestamp_from_mp_pl_names() -> None:
    assert parse_slide_timestamp("wyklad_450s.png") == 450
    assert parse_slide_timestamp("intro_0s.jpg") == 0
    assert parse_slide_timestamp("koniec_715s.webp") == 715


def test_timestamp_absent_is_none() -> None:
    assert parse_slide_timestamp("slide_03.png") is None
    assert parse_slide_timestamp("bez-czasu.jpg") is None


def test_natural_sort_gives_rising_time_sequence() -> None:
    """Sort naturalny nazw mp.pl daje rosnącą sekwencję czasową (nie leksykalną 31<715)."""
    names = ["s_715s.png", "s_3s.png", "s_31s.png", "s_0s.png"]
    slides = collect_slides(names)
    assert [s.timestamp_s for s in slides] == [0, 3, 31, 715]
    assert [s.index for s in slides] == [1, 2, 3, 4]


def test_generic_natural_sort() -> None:
    """Bez timestampów: sort naturalny (slide2 przed slide10, nie leksykalnie)."""
    slides = collect_slides(["slide10.png", "slide2.png", "slide1.png"])
    assert [s.filename for s in slides] == ["slide1.png", "slide2.png", "slide10.png"]
    assert all(s.timestamp_s is None for s in slides)


def test_non_images_filtered() -> None:
    slides = collect_slides(["a.png", "notes.txt", "data.json", "b.jpg"])
    assert [s.filename for s in slides] == ["a.png", "b.jpg"]


# ── Podłączenie / odczyt z folderu ────────────────────────────────────────────


def _make(tmp: Path, name: str) -> Path:
    path = tmp / name
    path.write_bytes(b"X")
    return path


def test_attach_copies_images_and_skips_non_images(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    material = tmp_path / "material"
    sources = [
        _make(src, "s_0s.png"),
        _make(src, "s_450s.png"),
        _make(src, "notatki.txt"),  # nie-obraz → pominięty
        _make(src, "manifest.json"),  # nie-obraz → pominięty
    ]
    slides = attach_slides(material, sources)

    copied = sorted(p.name for p in (material / SLIDES_DIRNAME).iterdir())
    assert copied == ["s_0s.png", "s_450s.png"]  # tylko obrazy skopiowane
    assert slides == [
        Slide("s_0s.png", 1, 0),
        Slide("s_450s.png", 2, 450),
    ]


def test_read_slides_from_disk(tmp_path: Path) -> None:
    material = tmp_path / "material"
    (material / SLIDES_DIRNAME).mkdir(parents=True)
    (material / SLIDES_DIRNAME / "s_5s.png").write_bytes(b"X")
    assert read_slides(material) == [Slide("s_5s.png", 1, 5)]
    # Brak folderu slides/ → pusta lista (nie wyjątek).
    assert read_slides(tmp_path / "inny") == []


# ── Integracja z biblioteką (metadata.json + SQLite + rescan) ─────────────────


def _store(tmp_path: Path) -> RecordingStore:
    db = tmp_path / "library.sqlite3"
    Database(db).migrate()
    return RecordingStore(db)


def _material(store: RecordingStore, lib: Path, title: str) -> tuple[int, Path, MaterialMetadata]:
    folder = lib / title
    meta = MaterialMetadata(title=title, created_at="t")
    write_metadata(folder, meta)
    rec_id = store.upsert_material(folder, meta)
    return rec_id, folder, meta


def test_store_add_slides_metadata_and_sqlite_roundtrip(tmp_path: Path) -> None:
    """Podłączenie: kopia do slides/, sekcja slides w metadata.json + indeks SQLite (round-trip)."""
    store = _store(tmp_path)
    rec_id, folder, meta = _material(store, tmp_path / "lib", "Wyklad")
    src = tmp_path / "src"
    src.mkdir()
    for name in ("s_0s.png", "s_450s.png", "notatki.txt"):
        _make(src, name)

    updated = store.add_slides(
        folder, meta, [src / "s_0s.png", src / "s_450s.png", src / "notatki.txt"]
    )
    assert updated.slides == (Slide("s_0s.png", 1, 0), Slide("s_450s.png", 2, 450))

    # metadata.json (źródło prawdy) ma sekcję slides z poprawnym index+timestamp_s.
    on_disk = read_metadata(folder).slides
    assert on_disk == updated.slides
    # SQLite zsynchronizowane (round-trip przez indeks).
    from_sql = store.get_material(rec_id)
    assert from_sql is not None and from_sql[1].slides == updated.slides
    # Nie-obraz nie trafił do slides/.
    assert not (folder / SLIDES_DIRNAME / "notatki.txt").exists()


def test_rescan_reconstructs_slides_from_folder(tmp_path: Path) -> None:
    """rescan odtwarza slides z folderu — plik dołożony ręcznie do slides/ pojawia się po skanie."""
    lib = tmp_path / "lib"
    store = _store(tmp_path)
    rec_id, folder, _meta = _material(store, lib, "Wyklad")
    initial = store.get_material(rec_id)
    assert initial is not None and initial[1].slides == ()

    # Użytkownik ręcznie wrzuca slajd do slides/ (bez udziału aplikacji).
    (folder / SLIDES_DIRNAME).mkdir(parents=True)
    (folder / SLIDES_DIRNAME / "s_31s.png").write_bytes(b"X")

    store.rescan(lib)
    # metadata.json zaktualizowany z dysku + indeks pokazuje slajd.
    assert read_metadata(folder).slides == (Slide("s_31s.png", 1, 31),)
    again = store.get_material(rec_id)
    assert again is not None and again[1].slides == (Slide("s_31s.png", 1, 31),)
