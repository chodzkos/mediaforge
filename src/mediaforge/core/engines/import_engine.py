"""ImporterEngine — import lokalnych plików A/V do biblioteki (implementacja AcquisitionEngine).

Kopiuje plik do folderu materiału („jeden materiał = jeden folder"), wyciąga audio
(FFmpeg, dla wideo), generuje miniaturę (FFmpeg) i zapisuje ``metadata.json`` (źródło
prawdy) zsynchronizowane z SQLite. Budowa komend FFmpeg to czyste, testowalne funkcje;
uruchamianie (subprocess) i kopiowanie są wstrzykiwane, więc orkiestracja jest testowalna
bez FFmpeg ani realnych plików wideo.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mediaforge.core.engines.base import (
    AcquireOptions,
    MediaArtifact,
    QualityOption,
    Source,
    SourceKind,
)
from mediaforge.core.engines.recorder import safe_filename
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.core.winutil import NO_WINDOW_FLAGS

VIDEO_EXTS: frozenset[str] = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi"})
AUDIO_EXTS: frozenset[str] = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})
SUPPORTED_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS


def is_supported(path: Path) -> bool:
    """Czy rozszerzenie pliku jest obsługiwane przy imporcie."""
    return path.suffix.lower() in SUPPORTED_EXTS


def is_video(path: Path) -> bool:
    """Czy plik jest kontenerem wideo (vs audio) — decyduje o ekstrakcji audio/miniaturze."""
    return path.suffix.lower() in VIDEO_EXTS


# ── Czyste buildery komend FFmpeg (testowalne bez uruchamiania) ───────────────


def build_extract_audio_command(src: Path, out: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    """Komenda ekstrakcji ścieżki audio z pliku wideo (AAC, bez wideo)."""
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "aac",
        "-b:a",
        "192k",
        str(out),
    ]


def build_extract_wav_command(src: Path, out: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    """Komenda konwersji źródła → 16 kHz mono PCM WAV (wejście dla whisper.cpp)."""
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(out),
    ]


def build_thumbnail_command(
    src: Path, out: Path, *, at_seconds: float = 3.0, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Komenda pobrania jednej klatki na miniaturę (``-ss`` przed ``-i`` = szybki seek)."""
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-ss",
        str(at_seconds),
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(out),
    ]


def build_probe_duration_command(src: Path, ffprobe: str = "ffprobe") -> list[str]:
    """Komenda ffprobe zwracająca samą długość (sekundy) na stdout."""
    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]


def parse_duration(ffprobe_stdout: str) -> float | None:
    """Parsuje długość z wyjścia ffprobe (sekundy) → float albo None."""
    text = ffprobe_stdout.strip().splitlines()
    if not text:
        return None
    try:
        return round(float(text[0].strip()), 2)
    except ValueError:
        return None


# ── Wstrzykiwane wykonawcy (subprocess) ───────────────────────────────────────

CommandRunner = Callable[[list[str]], int]
ProbeRunner = Callable[[list[str]], str]
CopyFn = Callable[[Path, Path], object]


def _default_runner(command: list[str]) -> int:
    proc = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW_FLAGS,
        check=False,
    )
    return proc.returncode


def _default_probe_runner(command: list[str]) -> str:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=NO_WINDOW_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class ImporterEngine:
    """Silnik importu lokalnych plików (Protocol :class:`AcquisitionEngine`).

    GUI używa :meth:`import_file` (z tytułem/metadanymi); :meth:`acquire` to wariant
    z interfejsu silnika (bierze ścieżkę z ``source.target``).
    """

    store: RecordingStore | None = None
    runner: CommandRunner = _default_runner
    probe_runner: ProbeRunner = _default_probe_runner
    copy_fn: CopyFn = shutil.copy2
    name: str = "importer"

    def can_handle(self, source: Source) -> bool:
        """Obsługuje lokalne pliki o wspieranym rozszerzeniu."""
        return source.kind is SourceKind.LOCAL_FILE and is_supported(Path(source.target))

    def probe(self, source: Source) -> list[QualityOption]:
        """Import zachowuje oryginał — jedna „jakość" (kopia 1:1)."""
        return [QualityOption(label="Import (kopia oryginału)")]

    def acquire(
        self,
        source: Source,
        opts: AcquireOptions,
        progress: Callable[[float, str], None],
    ) -> MediaArtifact:
        """Importuje plik wskazany w ``source.target`` do ``opts.output_dir``."""
        return self.import_file(Path(source.target), opts.output_dir, progress)

    def import_file(
        self,
        src: Path,
        library_root: Path,
        progress: Callable[[float, str], None],
        *,
        title: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        presenter: str | None = None,
        organizer: str | None = None,
    ) -> MediaArtifact:
        """Importuje pojedynczy plik: folder materiału + audio + miniatura + metadata.json.

        Zwraca :class:`MediaArtifact`. Folder materiału = ``library_root/<slug-tytułu>``;
        ``metadata.json`` (źródło prawdy) jest synchronizowane z SQLite (gdy jest ``store``).
        """
        title = title or src.stem
        slug = safe_filename(title)
        material_dir = library_root / slug
        material_dir.mkdir(parents=True, exist_ok=True)

        dest = material_dir / src.name
        self.copy_fn(src, dest)
        progress(0.4, "Skopiowano plik")

        video = is_video(src)
        audio_name: str | None = dest.name if not video else None
        thumb_name: str | None = None
        if video:
            audio_out = material_dir / f"{slug}.m4a"
            if self.runner(build_extract_audio_command(dest, audio_out)) == 0:
                audio_name = audio_out.name
            progress(0.7, "Wyodrębniono audio")
            thumb_out = material_dir / "thumbnail.jpg"
            if self.runner(build_thumbnail_command(dest, thumb_out)) == 0:
                thumb_name = thumb_out.name

        duration = parse_duration(self.probe_runner(build_probe_duration_command(dest)))

        meta = MaterialMetadata(
            title=title,
            created_at=_now(),
            source_type="import",
            presenter=presenter,
            organizer=organizer,
            category=category,
            tags=sorted(tags or []),
            duration=duration,
            video_path=dest.name if video else None,
            audio_path=audio_name,
            thumbnail_path=thumb_name,
        )
        write_metadata(material_dir, meta)
        if self.store is not None:
            self.store.upsert_material(material_dir, meta)
        progress(1.0, "Zaimportowano")

        return MediaArtifact(
            video_path=dest if video else None,
            audio_path=(material_dir / audio_name) if audio_name else None,
            metadata={"folder": str(material_dir), "duration_s": str(duration or "")},
        )
