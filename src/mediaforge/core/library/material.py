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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    summary_status: str = "none"
    status: str = "recorded"

    def __post_init__(self) -> None:
        # Tagi kanonicznie: bez pustych, bez duplikatów, posortowane — stabilny round-trip.
        self.tags = sorted({t.strip() for t in self.tags if t.strip()})

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
            "summary_status": self.summary_status,
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
            summary_status=str(data.get("summary_status", "none")),
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
    """Zapisuje ``metadata.json`` w folderze materiału (UTF-8, wcięcia, stabilny porządek)."""
    material_dir.mkdir(parents=True, exist_ok=True)
    path = metadata_path(material_dir)
    path.write_text(
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_metadata(material_dir: Path) -> MaterialMetadata:
    """Wczytuje ``metadata.json`` z folderu materiału (źródło prawdy)."""
    data = json.loads(metadata_path(material_dir).read_text(encoding="utf-8"))
    return MaterialMetadata.from_dict(data)
