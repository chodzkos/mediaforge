"""Parser i pobranie kanału RSS podcastu (stdlib xml.etree, bez sieci)."""

from __future__ import annotations

import pytest

from mediaforge.core.engines.podcast import fetch_podcast_feed, parse_podcast_rss

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>Podcast Anestezjologiczny</title>
  <item>
    <title>Odcinek 12: Sepsa</title>
    <enclosure url="https://cdn.example.com/ep12.mp3" type="audio/mpeg" length="52428800"/>
    <pubDate>Tue, 30 Jun 2026 06:00:00 +0000</pubDate>
    <itunes:duration>01:02:33</itunes:duration>
    <description>Omawiamy wytyczne.</description>
  </item>
  <item>
    <title>Zapowiedź wideo (bez audio)</title>
    <enclosure url="https://cdn.example.com/teaser.mp4" type="video/mp4"/>
  </item>
  <item>
    <title>Odcinek 11</title>
    <enclosure url="https://cdn.example.com/ep11.mp3" type="audio/mpeg"/>
    <itunes:summary>Opis z iTunes.</itunes:summary>
  </item>
</channel>
</rss>"""


def test_feed_and_audio_episodes() -> None:
    feed = parse_podcast_rss(SAMPLE)
    assert feed.title == "Podcast Anestezjologiczny"
    assert len(feed.episodes) == 2  # video-teaser pominięty
    ep = feed.episodes[0]
    assert ep.title == "Odcinek 12: Sepsa"
    assert ep.audio_url.endswith("ep12.mp3")
    assert ep.duration == "01:02:33"


def test_itunes_summary_fallback() -> None:
    feed = parse_podcast_rss(SAMPLE)
    assert feed.episodes[1].description == "Opis z iTunes."


def test_not_rss_raises() -> None:
    with pytest.raises(ValueError):
        parse_podcast_rss("<html>strona</html>")
    with pytest.raises(ValueError):
        parse_podcast_rss("nie xml w ogóle <<<")


def test_fetch_feed_uses_injected_fetcher() -> None:
    feed = fetch_podcast_feed("https://example.com/rss", fetcher=lambda _url, _timeout: SAMPLE)
    assert feed.title == "Podcast Anestezjologiczny" and len(feed.episodes) == 2


def test_fetch_feed_network_error_is_readable() -> None:
    def boom(_url: str, _timeout: float) -> str:
        raise OSError("connection refused")

    with pytest.raises(ValueError, match="Nie udało się pobrać"):
        fetch_podcast_feed("https://example.com/rss", fetcher=boom)
