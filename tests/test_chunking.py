"""Dzielnik transkryptu na kawałki (map-reduce): granice segmentów, budżet, czas, indeksy."""

from __future__ import annotations

from mediaforge.core.ai.chunking import Chunk, Segment, split_segments


def _seg(start: float, end: float, text: str) -> Segment:
    return Segment(start=start, end=end, text=text)


def test_short_input_single_chunk() -> None:
    """Krótkie wejście (mieści się w budżecie) → jeden kawałek, indeks 1, zakres czasu pełny."""
    segs = [_seg(0.0, 5.0, "Zdanie pierwsze."), _seg(5.0, 10.0, "Zdanie drugie.")]
    chunks = split_segments(segs, max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0] == Chunk(index=1, start=0.0, end=10.0, text="Zdanie pierwsze. Zdanie drugie.")


def test_splits_on_segment_boundaries_with_time_continuity() -> None:
    """10 segmentów po 100 zn., budżet 350 → 4 kawałki (3+3+3+1), każdy ≤ 350, czas ciągły."""
    segs = [_seg(i * 10.0, i * 10.0 + 9.0, "x" * 100) for i in range(10)]
    chunks = split_segments(segs, max_chars=350)

    assert [len(c.text) for c in chunks] == [302, 302, 302, 100]  # 3+3+3+1 segmentów
    assert all(len(c.text) <= 350 for c in chunks)
    # Ciągłość czasowa na granicy kawałków: koniec 1. kawałka i początek 2. z sąsiednich segmentów.
    assert chunks[0].end == 29.0 and chunks[1].start == 30.0
    # Tekst zachowany w całości (suma kawałków = sklejenie wszystkich segmentów).
    assert " ".join(c.text for c in chunks) == " ".join(s.text for s in segs)


def test_oversized_segment_gets_own_chunk_whole() -> None:
    """Segment dłuższy niż budżet → własny kawałek w CAŁOŚCI (nie tniemy zdania)."""
    segs = [_seg(0.0, 1.0, "krótki"), _seg(1.0, 60.0, "a" * 1000), _seg(60.0, 61.0, "koniec")]
    chunks = split_segments(segs, max_chars=200)
    assert [c.text for c in chunks] == ["krótki", "a" * 1000, "koniec"]
    assert chunks[1].start == 1.0 and chunks[1].end == 60.0


def test_empty_segments_skipped_time_from_nonempty() -> None:
    """Puste segmenty pomijane — także w liczeniu zakresu czasu (start/end z niepustych)."""
    segs = [
        _seg(0.0, 1.0, "   "),
        _seg(1.0, 5.0, "treść"),
        _seg(5.0, 9.0, ""),
        _seg(9.0, 12.0, "x"),
    ]
    chunks = split_segments(segs, max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0].text == "treść x"
    assert chunks[0].start == 1.0 and chunks[0].end == 12.0  # puste segmenty nie ruszają zakresu


def test_indices_are_sequential() -> None:
    """Indeksy kawałków są sekwencyjne 1..N (bez dziur, 1-based)."""
    segs = [_seg(i * 1.0, i * 1.0 + 0.5, "y" * 90) for i in range(9)]
    chunks = split_segments(segs, max_chars=100)  # każdy segment → własny kawałek
    assert [c.index for c in chunks] == list(range(1, 10))


def test_empty_input_no_chunks() -> None:
    """Puste wejście → pusta lista kawałków (nic do streszczenia)."""
    assert split_segments([], max_chars=1000) == []
