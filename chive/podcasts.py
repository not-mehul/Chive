"""Podcast directory search (Apple/iTunes, no key) and RSS feed parsing.

Feeds are standard RSS 2.0, parsed with the stdlib — no third-party dep.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlparse

from . import config
from .util import TTLCache

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# Parsed feeds, so paging and filtering a show don't re-download the whole
# (often multi-megabyte) feed on every page and keystroke.
FEED_CACHE = TTLCache(ttl=600, maxsize=16)


# ----- HTTP ------------------------------------------------------------------


def require_http_url(url: str) -> str:
    """Only http(s) may be fetched: urllib would otherwise happily read
    file:// (local files) or ftp:// on the strength of a client-supplied URL."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("feed URL must be an http(s) address")
    return url


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_http_url(newurl)   # a redirect must not escape to file:// etc.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpOnlyRedirects)


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(require_http_url(url),
                                 headers={"User-Agent": config.HTTP_USER_AGENT, "Accept": "*/*"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


# ----- search ----------------------------------------------------------------


def search_podcasts(term: str, limit: int = 12) -> list[dict[str, Any]]:
    q = urlencode({"term": term, "media": "podcast", "entity": "podcast", "limit": limit})
    try:
        data = json.loads(http_get(f"{config.ITUNES_SEARCH_URL}?{q}", timeout=20))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"podcast search failed: {exc}") from exc
    shows: list[dict[str, Any]] = []
    for r in data.get("results", []) if isinstance(data, dict) else []:
        if not r.get("feedUrl"):
            continue
        shows.append({
            "name": r.get("collectionName") or r.get("trackName") or "Podcast",
            "author": r.get("artistName"),
            "feed_url": r["feedUrl"],
            "artwork": r.get("artworkUrl600") or r.get("artworkUrl100"),
            "episode_count": r.get("trackCount"),
            "genre": r.get("primaryGenreName"),
        })
    return shows


# ----- feeds -----------------------------------------------------------------


def parse_duration(raw: Any) -> int | None:
    """itunes:duration — seconds ("3600") or a clock ("1:02:03")."""
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    try:
        nums = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def rss_date(raw: str | None) -> str | None:
    """RFC-822 pubDate → compact YYYYMMDD (the YouTube card format)."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).strftime("%Y%m%d")
    except (TypeError, ValueError, IndexError):
        return None


def parse_feed(raw: bytes) -> dict[str, Any]:
    """{podcast, artwork, episodes} from RSS bytes. Raises RuntimeError."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"could not parse feed: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("that doesn't look like a podcast RSS feed")
    show = (channel.findtext("title") or "").strip() or "Podcast"
    artwork = (channel.findtext("image/url") or "").strip() or None
    if not artwork:
        it_img = channel.find(f"{{{_ITUNES_NS}}}image")
        artwork = it_img.get("href") if it_img is not None else None
    episodes: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if not url:
            continue
        ep_img = item.find(f"{{{_ITUNES_NS}}}image")
        episodes.append({
            "video_id": (item.findtext("guid") or "").strip() or url,
            "url": url,
            "title": (item.findtext("title") or "Episode").strip(),
            "thumbnail": ep_img.get("href") if ep_img is not None else artwork,
            "duration": parse_duration(item.findtext(f"{{{_ITUNES_NS}}}duration")),
            "upload_date": rss_date(item.findtext("pubDate")),
            "collection": show,
        })
    return {"podcast": show, "artwork": artwork, "episodes": episodes}


def load_feed(feed_url: str) -> dict[str, Any]:
    cached = FEED_CACHE.get(feed_url)
    if cached is not None:
        return cached
    try:
        raw = http_get(feed_url, timeout=30)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"could not fetch feed: {exc}") from exc
    feed = parse_feed(raw)
    FEED_CACHE.set(feed_url, feed)
    return feed


def fetch_episodes(feed_url: str, page: int, page_size: int = config.PODCAST_PAGE_SIZE,
                   query: str | None = None) -> dict[str, Any]:
    """One page of a show's episodes, shaped like a scrape result so the UI
    grid renders both the same way."""
    feed = load_feed(feed_url)
    episodes = feed["episodes"]
    if query:
        ql = query.lower()
        episodes = [v for v in episodes if ql in v["title"].lower()]
    total = len(episodes)
    total_pages = max(1, -(-total // page_size))
    page = max(1, min(page, total_pages))
    window = [dict(v) for v in episodes[(page - 1) * page_size: page * page_size]]
    return {
        "podcast": feed["podcast"], "artwork": feed["artwork"], "feed_url": feed_url,
        "videos": window, "page": page, "page_size": page_size, "count": len(window),
        "total": total, "total_pages": total_pages, "source": "podcast",
    }
