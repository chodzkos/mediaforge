"""Parowanie slajd↔segmenty transkryptu: okna czasowe slajdów i przynależność segmentów (Qt-free).

Mapa slajd↔czas (z nazwy pliku, :mod:`core.library.slides`) + timestampy segmentów whispera
(:mod:`core.ai.transcribe`) → dla każdego slajdu Z timestampem okno czasu ``[ts, następny_ts)``
(ostatni slajd „do końca") i lista segmentów należących do tego okna. To wejście fazy 2 joba
notatek: komentarz prowadzącego per slajd powstaje ze streszczenia segmentów jego okna.

Przynależność segmentu rozstrzyga **środek** jego czasu ``(start + end) / 2`` — segment na
przełomie dwóch slajdów trafia tam, gdzie leży jego środek (środek dokładnie na granicy → okno
następne, bo okna są pół-otwarte ``[start, end)``). Slajdy BEZ timestampu nie dają okna (handler
robi dla nich osobną sekcję bez komentarza prowadzącego); brak slajdów → brak okien.

UWAGA: implementacja odtworzona ze specyfikacji zadania S6 (zachowanie + 5 przypadków testowych),
NIE jest to zweryfikowana wersja „z piaskownicy". Jeśli pojawi się ta druga — podmień 1:1
(kontrakt: te same nazwy ``SlideWindow`` / ``pair_slides_with_segments`` i te same testy).
"""

from __future__ import annotations

from dataclasses import dataclass

from mediaforge.core.ai.transcribe import Segment
from mediaforge.core.library.slides import Slide


@dataclass(frozen=True, slots=True)
class SlideWindow:
    """Slajd z timestampem + jego okno czasu i segmenty transkryptu należące do okna.

    ``end_s is None`` oznacza ostatni slajd („do końca nagrania"). ``segments`` są w kolejności
    wejściowej (chronologicznej z transkryptu), przefiltrowane po środku czasu każdego segmentu.
    """

    slide: Slide
    start_s: int
    end_s: int | None
    segments: tuple[Segment, ...]


def pair_slides_with_segments(slides: list[Slide], segments: list[Segment]) -> list[SlideWindow]:
    """Buduje okna czasowe slajdów Z timestampem i przypisuje segmenty po środku ich czasu.

    Slajdy bez timestampu są pomijane (brak okna). Wejście może być nieposortowane — slajdy z
    timestampem sortujemy rosnąco po czasie, więc kolejność okien = kolejność w nagraniu. Okno
    slajdu ``i`` to ``[ts_i, ts_{i+1})``; ostatnie okno jest otwarte (``end_s is None``). Segment
    trafia do okna, którego przedział zawiera środek jego czasu; segment ze środkiem przed
    pierwszym slajdem nie trafia do żadnego okna (pomijany).
    """
    timed = sorted(
        ((s.timestamp_s, s) for s in slides if s.timestamp_s is not None),
        key=lambda pair: pair[0],
    )
    if not timed:
        return []

    bounds: list[tuple[Slide, int, int | None]] = []
    for i, (start, slide) in enumerate(timed):
        end = timed[i + 1][0] if i + 1 < len(timed) else None
        bounds.append((slide, start, end))

    buckets: list[list[Segment]] = [[] for _ in bounds]
    for seg in segments:
        idx = _window_index(bounds, (seg.start + seg.end) / 2.0)
        if idx is not None:
            buckets[idx].append(seg)

    return [
        SlideWindow(slide=slide, start_s=start, end_s=end, segments=tuple(bucket))
        for (slide, start, end), bucket in zip(bounds, buckets, strict=True)
    ]


def _window_index(bounds: list[tuple[Slide, int, int | None]], midpoint: float) -> int | None:
    """Indeks okna zawierającego ``midpoint`` (``[start, end)``; ostatnie otwarte) albo ``None``."""
    for i, (_slide, start, end) in enumerate(bounds):
        if midpoint >= start and (end is None or midpoint < end):
            return i
    return None
