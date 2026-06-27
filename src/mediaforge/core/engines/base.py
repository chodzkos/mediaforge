"""Kontrakt silnika akwizycji.

Wzorzec strategii: jeden Protocol, wiele implementacji (RecorderEngine,
ImporterEngine, DownloaderEngine). Selektor dobiera silnik wg źródła; użytkownik
może nadpisać ręcznie. Analogicznie do wielosilnikowego pdf2md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class SourceKind(StrEnum):
    SCREEN = "screen"  # nagrywanie ekranu / audio
    LOCAL_FILE = "local"  # import pliku z dysku
    URL = "url"  # pobranie (yt-dlp, etap S5, tylko bez DRM)


@dataclass(slots=True)
class Source:
    kind: SourceKind
    target: str  # ścieżka pliku, URL albo identyfikator regionu/okna


@dataclass(slots=True)
class QualityOption:
    label: str
    video_codec: str | None = None
    audio_codec: str | None = None
    resolution: str | None = None
    fps: int | None = None
    bitrate_kbps: int | None = None
    audio_only: bool = False


@dataclass(slots=True)
class AcquireOptions:
    quality: QualityOption
    output_dir: Path
    audio_only: bool = False


@dataclass(slots=True)
class MediaArtifact:
    video_path: Path | None
    audio_path: Path | None
    metadata: dict[str, str] = field(default_factory=dict)


# Callback postępu: (frakcja 0..1, komunikat).
ProgressCb = Callable[[float, str], None]


@runtime_checkable
class AcquisitionEngine(Protocol):
    """Każdy silnik pozyskuje surowy materiał do folderu i zwraca MediaArtifact."""

    name: str

    def can_handle(self, source: Source) -> bool:
        """Czy ten silnik obsługuje dane źródło."""
        ...

    def probe(self, source: Source) -> list[QualityOption]:
        """Dostępne opcje jakości dla źródła."""
        ...

    def acquire(
        self,
        source: Source,
        opts: AcquireOptions,
        progress: ProgressCb,
    ) -> MediaArtifact:
        """Wykonaj akwizycję. Implementacje muszą respektować LEGAL_BOUNDARIES.md."""
        ...
