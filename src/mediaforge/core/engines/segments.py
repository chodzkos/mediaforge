"""Segmentacja crash-safe i odzysk nagrania ze zsegmentowanych plików.

Strategia odporności: FFmpeg pisze nagranie jako serię segmentów (``seg_000.mkv``,
``seg_001.mkv``, …). Każdy segment jest finalizowany przy rotacji, więc nagłe zamknięcie
(crash, ubity proces) gubi co najwyżej bieżący, niedokończony segment — wszystkie
wcześniejsze są kompletne i odtwarzalne. Odzysk = sklejenie ważnych segmentów w jeden
plik wynikowy demuxerem ``concat`` (kopia strumieni, bez transkodowania).

Dodatkowo MKV jest tolerancyjny na obcięcie: nawet ostatni, przerwany segment zwykle da
się odtworzyć/zremuxować — ale dla bezpieczeństwa odzysk pomija segmenty zerowej długości.

Ten moduł jest czystym stdlib (bez subprocess do odzysku poza zbudowaniem komendy concat),
więc test odporności może symulować przerwanie, tworząc pliki segmentów na dysku.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SEGMENT_PREFIX = "seg_"
SEGMENT_GLOB = f"{SEGMENT_PREFIX}*"


def segment_pattern(work_dir: Path, container: str = "mkv") -> str:
    """Wzorzec ścieżki segmentu dla FFmpeg (``-f segment``), np. ``.../seg_%03d.mkv``."""
    return str(work_dir / f"{SEGMENT_PREFIX}%03d.{container}")


def list_segments(work_dir: Path) -> list[Path]:
    """Wszystkie pliki segmentów w katalogu roboczym, posortowane wg numeru w nazwie."""
    if not work_dir.is_dir():
        return []
    return sorted(work_dir.glob(SEGMENT_GLOB), key=lambda p: p.name)


def valid_segments(work_dir: Path) -> list[Path]:
    """Segmenty nadające się do sklejenia — pomija puste (zerowej długości) pliki.

    Pusty plik to typowo segment otwarty tuż przed crashem (FFmpeg nie zdążył nic
    zapisać). Niezerowe segmenty są traktowane jako kompletne/odtwarzalne.
    """
    return [p for p in list_segments(work_dir) if p.is_file() and p.stat().st_size > 0]


def write_concat_list(segments: list[Path], list_path: Path) -> Path:
    """Zapisuje plik listy dla demuxera ``concat`` (po jednej ścieżce na linię).

    Ścieżki w cudzysłowach z eskejpem apostrofu — format wymagany przez FFmpeg concat.
    """
    lines = []
    for seg in segments:
        escaped = str(seg.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


def build_concat_command(list_path: Path, output: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    """Komenda FFmpeg sklejająca segmenty z listy w jeden plik (kopia strumieni)."""
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(output),
    ]


@dataclass(slots=True)
class RecoveryResult:
    """Wynik analizy odzysku nagrania ze zsegmentowanych plików.

    Attributes:
        segments: ważne (niepuste) segmenty w kolejności sklejania.
        recoverable: czy jest cokolwiek do odzyskania (≥1 ważny segment).
        list_path: zapisany plik listy concat (None, gdy nic do odzysku).
        command: komenda FFmpeg concat (pusta, gdy nic do odzysku).
        dropped: segmenty pominięte jako uszkodzone/puste.
    """

    segments: list[Path]
    recoverable: bool
    list_path: Path | None = None
    command: list[str] = field(default_factory=list)
    dropped: list[Path] = field(default_factory=list)


def plan_recovery(work_dir: Path, output: Path, ffmpeg: str = "ffmpeg") -> RecoveryResult:
    """Przygotowuje plan odzysku: ważne segmenty + zapisana lista + komenda concat.

    Nie uruchamia FFmpeg — tylko analizuje katalog i buduje komendę. Gdy jest ≥1 ważny
    segment, zapisuje plik listy (``concat.txt``) i komendę; inaczej ``recoverable=False``.
    """
    all_segments = list_segments(work_dir)
    good = valid_segments(work_dir)
    dropped = [p for p in all_segments if p not in good]
    if not good:
        return RecoveryResult(segments=[], recoverable=False, dropped=dropped)
    list_path = write_concat_list(good, work_dir / "concat.txt")
    command = build_concat_command(list_path, output, ffmpeg)
    return RecoveryResult(
        segments=good,
        recoverable=True,
        list_path=list_path,
        command=command,
        dropped=dropped,
    )
