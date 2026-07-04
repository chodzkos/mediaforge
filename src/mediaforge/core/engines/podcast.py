"""Parser kanałów RSS podcastów — czysta funkcja na stdlib (xml.etree), bez feedparser.

Obsługuje RSS 2.0 z enclosure audio + popularne pola iTunes (duration, summary).
Odporny na braki pól: odcinek bez enclosure audio jest pomijany (nie ma czego pobrać).

Pobranie feedu (:func:`fetch_podcast_feed`) idzie przez ``urllib.request`` z timeoutem (jak
transport w summarize) — błąd sieci zamieniany na czytelny ``ValueError``. Fetcher jest
wstrzykiwalny, więc parsowanie i orkiestracja są testowalne bez sieci.
"""

from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@dataclass(frozen=True)
class PodcastEpisode:
    title: str
    audio_url: str
    published: str  # surowy pubDate (RFC 822) — parsowanie daty po stronie konsumenta
    duration: str  # surowe itunes:duration ("HH:MM:SS" albo sekundy) lub ""
    description: str


@dataclass(frozen=True)
class PodcastFeed:
    title: str
    episodes: tuple[PodcastEpisode, ...]


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_podcast_rss(xml_text: str) -> PodcastFeed:
    """Parsuje XML kanału RSS → tytuł + odcinki z audio. Rzuca ValueError na nie-RSS."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"Niepoprawny XML kanału RSS: {e}") from e
    channel = root.find("channel")
    if channel is None:
        raise ValueError("To nie jest kanał RSS (brak <channel>)")
    episodes: list[PodcastEpisode] = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        url = enclosure.get("url", "") if enclosure is not None else ""
        mime = enclosure.get("type", "") if enclosure is not None else ""
        if not url or (mime and not mime.startswith("audio/")):
            continue  # bez audio nie ma czego pobrać
        episodes.append(
            PodcastEpisode(
                title=_text(item.find("title")) or "(bez tytułu)",
                audio_url=url,
                published=_text(item.find("pubDate")),
                duration=_text(item.find(f"{_ITUNES}duration")),
                description=_text(item.find("description"))
                or _text(item.find(f"{_ITUNES}summary")),
            )
        )
    return PodcastFeed(
        title=_text(channel.find("title")) or "(bez nazwy)", episodes=tuple(episodes)
    )


# ── Pobranie feedu (urllib, wstrzykiwalny fetcher) ────────────────────────────

FeedFetcher = Callable[[str, float], str]


def _default_fetch(url: str, timeout: float) -> str:
    """Pobiera XML feedu przez ``urllib`` (User-Agent, bo część serwerów odrzuca domyślny)."""
    request = urllib.request.Request(url, headers={"User-Agent": "mediaforge/podcast"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return str(resp.read().decode(charset, errors="replace"))


def fetch_podcast_feed(
    url: str, *, timeout: float = 15.0, fetcher: FeedFetcher = _default_fetch
) -> PodcastFeed:
    """Pobiera i parsuje kanał RSS; błąd sieci → czytelny ``ValueError`` z URL-em."""
    try:
        xml_text = fetcher(url, timeout)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise ValueError(f"Nie udało się pobrać kanału RSS ({url}): {exc}") from exc
    return parse_podcast_rss(xml_text)
