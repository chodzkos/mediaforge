"""Parowanie slajd↔segmenty: okna czasowe i przynależność segmentów (środek czasu decyduje)."""

from __future__ import annotations

from mediaforge.core.ai.pairing import SlideWindow, pair_slides_with_segments
from mediaforge.core.ai.transcribe import Segment
from mediaforge.core.library.slides import Slide


def _slide(index: int, ts: int | None) -> Slide:
    return Slide(filename=f"slajd_{index}.png", index=index, timestamp_s=ts)


def _seg(start: float, end: float, text: str = "x") -> Segment:
    return Segment(start=start, end=end, text=text)


def test_windows_and_membership() -> None:
    """Okna [ts, next_ts) z ostatnim otwartym; segmenty trafiają do okna swojego środka czasu."""
    slides = [_slide(1, 0), _slide(2, 100), _slide(3, 200)]
    segments = [
        _seg(10, 20, "a"),  # środek 15 → slajd 1 [0,100)
        _seg(90, 110, "b"),  # środek 100 → slajd 2 [100,200) (granica → następne okno)
        _seg(150, 160, "c"),  # środek 155 → slajd 2
        _seg(210, 250, "d"),  # środek 230 → slajd 3 [200,∞)
    ]
    windows = pair_slides_with_segments(slides, segments)

    assert [(w.start_s, w.end_s) for w in windows] == [(0, 100), (100, 200), (200, None)]
    assert [s.text for s in windows[0].segments] == ["a"]
    assert [s.text for s in windows[1].segments] == ["b", "c"]
    assert [s.text for s in windows[2].segments] == ["d"]


def test_segment_on_boundary_assigned_by_midpoint() -> None:
    """Segment na przełomie dwóch slajdów: o przynależności decyduje ŚRODEK jego czasu."""
    slides = [_slide(1, 0), _slide(2, 100)]
    # Ten sam przełom (segment przecina granicę 100), różny środek → różne okno.
    left = _seg(80, 118, "left")  # środek 99 < 100 → slajd 1
    right = _seg(82, 120, "right")  # środek 101 >= 100 → slajd 2
    windows = pair_slides_with_segments(slides, [left, right])

    assert [s.text for s in windows[0].segments] == ["left"]
    assert [s.text for s in windows[1].segments] == ["right"]


def test_unsorted_slides_sorted_by_time() -> None:
    """Nieposortowane wejście slajdów → okna po czasie; sort wpływa też na PRZYNALEŻNOŚĆ segmentu.

    Slajdy podane w kolejności indeksów [(3,200),(1,0),(2,40)] (mp.pl daje posortowane, ale ręczny
    attach-slides z dziwnymi nazwami — niekoniecznie). Segment ze środkiem 50 należy do okna
    slajdu z ts=40 (drugi w czasie), NIE do slajdu podanego jako pierwszy na wejściu.
    """
    slides = [_slide(3, 200), _slide(1, 0), _slide(2, 40)]
    windows = pair_slides_with_segments(slides, [_seg(45, 55, "mid50")])

    # Okna w porządku CZASOWYM (nie wejściowym) — granice zależą od posortowania.
    assert [w.slide.timestamp_s for w in windows] == [0, 40, 200]
    assert [w.end_s for w in windows] == [40, 200, None]
    # Segment (środek 50) trafia do okna [40, 200), czyli slajdu ts=40 — dowód, że sort działa.
    assert [s.text for s in windows[0].segments] == []
    assert [s.text for s in windows[1].segments] == ["mid50"]
    assert [s.text for s in windows[2].segments] == []


def test_slides_without_timestamp_yield_no_windows() -> None:
    """Slajdy bez timestampu nie dają okien; w mieszanym zestawie okna tylko dla otagowanych."""
    # Wszystkie bez timestampu → brak okien.
    assert pair_slides_with_segments([_slide(1, None), _slide(2, None)], [_seg(0, 10)]) == []
    # Mieszane: tylko slajd z timestampem dostaje okno.
    windows = pair_slides_with_segments([_slide(1, None), _slide(2, 50)], [_seg(60, 70, "a")])
    assert len(windows) == 1
    assert windows[0].slide.index == 2
    assert [s.text for s in windows[0].segments] == ["a"]


def test_no_slides_no_windows() -> None:
    """Brak slajdów → brak okien (pusta lista)."""
    assert pair_slides_with_segments([], [_seg(0, 10)]) == []


def test_segment_before_first_slide_is_dropped() -> None:
    """Segment ze środkiem przed pierwszym slajdem nie trafia do żadnego okna (pomijany)."""
    slides = [_slide(1, 100), _slide(2, 200)]
    windows = pair_slides_with_segments(slides, [_seg(0, 40, "early")])  # środek 20 < 100
    assert isinstance(windows[0], SlideWindow)
    assert all(len(w.segments) == 0 for w in windows)
