"""Slajdy podłączane z folderu: parser kolejności/timestampu + kopia do folderu materiału.

Qt-free. Parser (:func:`collect_slides`) filtruje obrazy, sortuje naturalnie i wyciąga
sekundę z nazwy (np. mp.pl: ``..._450s.png``) — to mapa slajd↔czas pod S6 (slajd↔segment
transkryptu). Źródło prawdy = pliki w podfolderze ``slides/`` materiału; ``metadata.json``
i SQLite tylko je indeksują (round-trip odtwarzalny z dysku przez rescan).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SLIDES_DIRNAME = "slides"

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_TIME_RE = re.compile(r"_(\d+)s(?=\.|_|$)")


@dataclass(frozen=True)
class Slide:
    filename: str
    index: int  # kolejność wyświetlania (1-based)
    timestamp_s: int | None  # sekunda w nagraniu, gdy nazwa ją niesie


def parse_slide_timestamp(filename: str) -> int | None:
    """Wyciąga sekundę z nazwy typu '..._450s.png' albo None."""
    m = _TIME_RE.search(filename)
    return int(m.group(1)) if m else None


def _natural_key(name: str) -> list[object]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def collect_slides(filenames: list[str]) -> list[Slide]:
    """Filtruje obrazy, sortuje naturalnie, numeruje i wyciąga timestampy."""
    images = sorted(
        (f for f in filenames if Path(f).suffix.lower() in _IMAGE_EXT),
        key=_natural_key,
    )
    return [
        Slide(filename=f, index=i, timestamp_s=parse_slide_timestamp(f))
        for i, f in enumerate(images, start=1)
    ]


# ── Serializacja (metadata.json / SQLite) ─────────────────────────────────────


def slide_to_dict(slide: Slide) -> dict[str, Any]:
    """Słownik slajdu do ``metadata.json`` (``timestamp_s=null`` gdy nazwa nie niosła czasu)."""
    return {"filename": slide.filename, "index": slide.index, "timestamp_s": slide.timestamp_s}


def slide_from_dict(data: Mapping[str, Any]) -> Slide:
    """Buduje :class:`Slide` ze słownika (odporny na braki/nadmiar kluczy)."""
    raw_ts = data.get("timestamp_s")
    timestamp_s = int(raw_ts) if isinstance(raw_ts, int) else None
    return Slide(
        filename=str(data.get("filename", "")),
        index=int(data.get("index", 0)),
        timestamp_s=timestamp_s,
    )


# ── Podłączenie / odczyt z folderu materiału ──────────────────────────────────

CopyFn = Callable[[Path, Path], object]


def read_slides(material_dir: Path) -> list[Slide]:
    """Skanuje podfolder ``slides/`` materiału i odtwarza listę slajdów (źródło prawdy = dysk)."""
    slides_dir = material_dir / SLIDES_DIRNAME
    if not slides_dir.is_dir():
        return []
    names = [p.name for p in slides_dir.iterdir() if p.is_file()]
    return collect_slides(names)


def attach_slides(
    material_dir: Path, sources: list[Path], *, copy_fn: CopyFn = shutil.copy2
) -> list[Slide]:
    """Kopiuje pliki-obrazy do ``slides/`` materiału (nie-obrazy pomija) i zwraca listę slajdów.

    Kopiujemy, NIE linkujemy — folder materiału ma być samowystarczalny i przenośny (NAS).
    Nie-obrazy (txt/json…) są pomijane przy kopiowaniu (do ``slides/`` trafiają tylko slajdy).
    """
    slides_dir = material_dir / SLIDES_DIRNAME
    slides_dir.mkdir(parents=True, exist_ok=True)
    for src in sources:
        path = Path(src)
        if path.suffix.lower() in _IMAGE_EXT and path.is_file():
            copy_fn(path, slides_dir / path.name)
    return read_slides(material_dir)
