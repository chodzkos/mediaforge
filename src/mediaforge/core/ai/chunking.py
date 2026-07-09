"""Dzielnik transkryptu na kawałki po granicach segmentów whispera (Qt-free).

Usuwa strukturalny sufit długości streszczenia (map-reduce): materiał dzielony jest
WYŁĄCZNIE na granicach segmentów whisper.cpp — nigdy w środku zdania — więc każdy kawałek
jest samodzielnym, sensownym fragmentem do streszczenia.

Reużywamy :class:`~mediaforge.core.ai.transcribe.Segment` (start/end/text) — jedno źródło
typu segmentu w całym module AI. Nowy jest tu tylko :class:`Chunk` (grupa segmentów + zakres
czasu), którego map-reduce używa jako jednostki jednego wywołania gatewaya.
"""

from __future__ import annotations

from dataclasses import dataclass

from mediaforge.core.ai.transcribe import Segment

__all__ = ["Chunk", "Segment", "split_segments"]


@dataclass(frozen=True, slots=True)
class Chunk:
    """Kawałek transkryptu: sklejony tekst grupy segmentów + zakres czasu (indeks 1-based)."""

    index: int  # 1-based — kolejność kawałka w materiale
    start: float  # początek pierwszego segmentu (sekundy)
    end: float  # koniec ostatniego segmentu (sekundy)
    text: str  # sklejony tekst segmentów (spacja), bez timestampów


def split_segments(segments: list[Segment], max_chars: int) -> list[Chunk]:
    """Tnie segmenty na kawałki ``<= max_chars``, WYŁĄCZNIE na granicach segmentów.

    Reguły (celowo proste i przewidywalne):

    * granica kawałka wypada tylko MIĘDZY segmentami — zdania nie są dzielone;
    * pojedynczy segment dłuższy niż ``max_chars`` trafia do WŁASNEGO kawałka w całości
      (nie tniemy go — lepszy jeden za duży kawałek niż ucięte zdanie);
    * puste segmenty (po ``strip``) są pomijane, także w liczeniu zakresu czasu;
    * niepuste wejście daje >= 1 kawałek; puste wejście daje pustą listę.

    Długość liczona po ``len`` tekstu segmentów + separatory (spacje) — to samo przybliżenie
    rozmiaru promptu, którego używa reszta ścieżki streszczenia.
    """
    chunks: list[Chunk] = []
    buf: list[Segment] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append(
                Chunk(
                    index=len(chunks) + 1,
                    start=buf[0].start,
                    end=buf[-1].end,
                    text=" ".join(s.text.strip() for s in buf),
                )
            )
            buf, buf_len = [], 0

    for seg in segments:
        t = seg.text.strip()
        if not t:
            continue
        # Nowy segment nie mieści się w bieżącym buforze → domknij kawałek na granicy segmentu.
        if buf and buf_len + len(t) + 1 > max_chars:
            flush()
        buf.append(seg)
        buf_len += len(t) + 1
    flush()
    return chunks
