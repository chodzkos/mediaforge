"""Metadane materiału + układ „jeden materiał = jeden folder" z ``metadata.json``.

`metadata.json` w folderze materiału jest **źródłem prawdy**; tabela SQLite
(:mod:`core.library.recordings`) to indeks nad nim (do listy/filtrów/podglądu).
Ścieżki plików w metadanych są **względne do folderu materiału** (sama nazwa pliku),
żeby folder był przenośny — bezwzględne ścieżki rozwiązuje się dopiero przy użyciu.

Round-trip: ``MaterialMetadata`` ↔ ``metadata.json`` ↔ wiersz SQLite musi zachować
wszystkie pola (tytuł, data, źródło, prowadzący, organizator, kategoria, tagi, długość,
statusy transkrypcji/streszczenia).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaforge.core.library.slides import Slide, slide_from_dict, slide_to_dict

METADATA_FILENAME = "metadata.json"


@dataclass(slots=True)
class MaterialMetadata:
    """Pełne metadane materiału (zapisywane do ``metadata.json``)."""

    title: str
    created_at: str
    source_type: str = "import"  # import / screen / download
    source_url: str | None = None
    presenter: str | None = None  # prowadzący
    organizer: str | None = None  # organizator
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    duration: float | None = None
    video_path: str | None = None  # nazwa pliku w folderze materiału (względna)
    audio_path: str | None = None
    thumbnail_path: str | None = None
    transcript_status: str = "none"  # none / done
    transcript_json: str | None = None  # plik .json whisper.cpp (w folderze materiału)
    transcript_srt: str | None = None
    summary_status: str = "none"
    summary_path: str | None = None  # plik streszczenia .md w folderze materiału
    # Streszczenia cząstkowe (map-reduce długiego materiału) — plik obok summary.md, obecny
    # TYLKO gdy streszczenie szło ścieżką chunked. Brak = ścieżka pojedyncza (jeden request).
    summary_parts_path: str | None = None
    # Notatka per slajd (S6): notes.md w folderze materiału, gdy materiał ma slajdy + transkrypt.
    notes_status: str = "none"  # none / done
    notes_path: str | None = None  # plik notes.md w folderze materiału
    # TWARDA GRANICA prywatności (fail-safe): materiał jest wrażliwy, DOPÓKI użytkownik jawnie
    # nie ustawi cloud_ok=True. Brak pola w metadata.json = False (zapomnienie = bezpieczne).
    cloud_ok: bool = False
    # Slajdy podłączone z folderu ``slides/`` (mapa slajd↔czas pod S6). Źródło prawdy = dysk;
    # tu tylko indeks. Kolejność wg ``index`` (nie sortujemy — collect_slides już ponumerował).
    slides: tuple[Slide, ...] = ()
    status: str = "recorded"

    def __post_init__(self) -> None:
        # Tagi kanonicznie: bez pustych, bez duplikatów, posortowane — stabilny round-trip.
        self.tags = sorted({t.strip() for t in self.tags if t.strip()})
        # Slajdy z listy → krotka (niemutowalna, porównywalna w round-tripie).
        self.slides = tuple(self.slides)

    def to_dict(self) -> dict[str, Any]:
        """Słownik do serializacji JSON (tagi posortowane dla stabilnego pliku)."""
        return {
            "title": self.title,
            "created_at": self.created_at,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "presenter": self.presenter,
            "organizer": self.organizer,
            "category": self.category,
            "tags": sorted(self.tags),
            "duration": self.duration,
            "video_path": self.video_path,
            "audio_path": self.audio_path,
            "thumbnail_path": self.thumbnail_path,
            "transcript_status": self.transcript_status,
            "transcript_json": self.transcript_json,
            "transcript_srt": self.transcript_srt,
            "summary_status": self.summary_status,
            "summary_path": self.summary_path,
            "summary_parts_path": self.summary_parts_path,
            "notes_status": self.notes_status,
            "notes_path": self.notes_path,
            "cloud_ok": self.cloud_ok,
            "slides": [slide_to_dict(s) for s in self.slides],
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaterialMetadata:
        """Buduje metadane ze słownika (odporne na brakujące/nadmiarowe klucze)."""
        raw_tags = data.get("tags") or []
        tags = sorted(str(t) for t in raw_tags if str(t).strip())
        return cls(
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", "")),
            source_type=str(data.get("source_type", "import")),
            source_url=_opt_str(data.get("source_url")),
            presenter=_opt_str(data.get("presenter")),
            organizer=_opt_str(data.get("organizer")),
            category=_opt_str(data.get("category")),
            tags=tags,
            duration=_opt_float(data.get("duration")),
            video_path=_opt_str(data.get("video_path")),
            audio_path=_opt_str(data.get("audio_path")),
            thumbnail_path=_opt_str(data.get("thumbnail_path")),
            transcript_status=str(data.get("transcript_status", "none")),
            transcript_json=_opt_str(data.get("transcript_json")),
            transcript_srt=_opt_str(data.get("transcript_srt")),
            summary_status=str(data.get("summary_status", "none")),
            summary_path=_opt_str(data.get("summary_path")),
            summary_parts_path=_opt_str(data.get("summary_parts_path")),
            notes_status=str(data.get("notes_status", "none")),
            notes_path=_opt_str(data.get("notes_path")),
            # Brak pola = False: zapomnienie zgody jest bezpieczne (materiał zostaje lokalnie).
            cloud_ok=bool(data.get("cloud_ok", False)),
            slides=tuple(
                slide_from_dict(s) for s in (data.get("slides") or []) if isinstance(s, dict)
            ),
            status=str(data.get("status", "recorded")),
        )


def _opt_str(value: Any) -> str | None:
    """Normalizuje wartość do niepustego str albo None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def metadata_path(material_dir: Path) -> Path:
    """Ścieżka pliku ``metadata.json`` w folderze materiału."""
    return material_dir / METADATA_FILENAME


def write_metadata(material_dir: Path, meta: MaterialMetadata) -> Path:
    """Zapisuje ``metadata.json`` ATOMOWO (UTF-8, wcięcia, stabilny porządek).

    Źródło prawdy nie może się urwać w połowie: piszemy do pliku tymczasowego w TYM SAMYM
    katalogu (ten sam filesystem → ``os.replace`` jest atomowy), a dopiero potem podmieniamy
    docelowy. Przerwany zapis (crash / brak miejsca) zostawia stary, kompletny ``metadata.json``
    zamiast obciętego. Przy błędzie sprzątamy tmp i propagujemy wyjątek.
    """
    material_dir.mkdir(parents=True, exist_ok=True)
    path = metadata_path(material_dir)
    payload = json.dumps(meta.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=material_dir, prefix=".metadata-", suffix=".json.tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)  # atomowa podmiana (ten sam katalog/filesystem)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_metadata(material_dir: Path) -> MaterialMetadata:
    """Wczytuje ``metadata.json`` z folderu materiału (źródło prawdy).

    Uszkodzony JSON → czytelny ``ValueError`` ze ścieżką. Skan biblioteki
    (:meth:`RecordingStore.rescan`) łapie go per-materiał i pomija ten materiał z logiem,
    zamiast wywracać cały skan.
    """
    text = metadata_path(material_dir).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Uszkodzony metadata.json w {material_dir}") from exc
    return MaterialMetadata.from_dict(data)
