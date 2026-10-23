"""YouTube channel scraping, channel search, filtering and sorting."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from . import config, ytdlp
from .util import TTLCache

# Per-URL cache of flat-playlist entries. yt-dlp can only walk a channel's
# continuation tokens from the top, so every fetch is a "1:end" scrape and the
# deepest one already contains every shallower page — caching it makes
# back-navigation, Shorts toggling and re-filtering instant.
SCRAPE_CACHE = TTLCache(ttl=300, maxsize=16)

FILTER_KEYS = ("query", "duration_min", "duration_max", "views_min", "views_max",
               "date_from", "date_to")


def thumbnail_for(video_id: str, hinted: str | None) -> str | None:
    if hinted:
        return hinted
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None


def is_short(entry_url: str | None) -> bool:
    # The `/shorts/` URL path is the only reliable signal: plenty of regular
    # uploads run under a minute, and the Shorts length cap is now 3 minutes.
    return bool(entry_url and "/shorts/" in entry_url)


def entry_to_video(entry: dict[str, Any]) -> dict[str, Any] | None:
    vid = entry.get("id")
    if not vid:
        return None
    thumbs = entry.get("thumbnails") or []
    thumb = thumbs[-1].get("url") if isinstance(thumbs, list) and thumbs else entry.get("thumbnail")
    return {
        "video_id": vid,
        "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
        "title": entry.get("title") or vid,
        "thumbnail": thumbnail_for(vid, thumb),
        "duration": entry.get("duration"),
        "upload_date": entry.get("upload_date"),
        "view_count": entry.get("view_count"),
        "channel": entry.get("channel") or entry.get("uploader"),
    }


def norm_date(s: str | None) -> str | None:
    """'YYYY-MM-DD' or 'YYYYMMDD' → compact 'YYYYMMDD'."""
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return digits[:8] if len(digits) >= 8 else None


def passes_filters(v: dict[str, Any], f: dict[str, Any]) -> bool:
    """A missing field on the entry passes that filter — flat-playlist often
    omits view_count / upload_date, and an archival tool should not silently
    hide videos it merely lacks metadata for."""
    q = f.get("query")
    if q and q.lower() not in (v.get("title") or "").lower():
        return False
    for field, lo_key, hi_key in (("duration", "duration_min", "duration_max"),
                                  ("view_count", "views_min", "views_max"),
                                  ("upload_date", "date_from", "date_to")):
        val = v.get(field)
        if val in (None, ""):
            continue
        lo, hi = f.get(lo_key), f.get(hi_key)
        if lo not in (None, "") and val < lo:
            return False
        if hi not in (None, "") and val > hi:
            return False
    return True


def _key_desc(field: str):
    return lambda v: v.get(field) if v.get(field) is not None else -1


def _key_asc(field: str):
    return lambda v: v.get(field) if v.get(field) is not None else float("inf")


def sort_videos(videos: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Incoming order is the channel's native newest-first order."""
    if sort == "oldest":
        return list(reversed(videos))
    keyed = {
        "longest": (_key_desc("duration"), True),
        "shortest": (_key_asc("duration"), False),
        "most_viewed": (_key_desc("view_count"), True),
        "least_viewed": (_key_asc("view_count"), False),
    }.get(sort)
    if keyed is None:
        return videos
    key, reverse = keyed
    return sorted(videos, key=key, reverse=reverse)


def filters_active(f: dict[str, Any], sort: str) -> bool:
    return any(f.get(k) not in (None, "") for k in FILTER_KEYS) or bool(sort and sort != "newest")


def fetch_entries(url: str, end: int) -> list[dict[str, Any]]:
    """Flat-playlist entries 1..end for `url`, from cache when deep enough.

    For channels, `--playlist-items 51:100` often returns nothing because
    yt-dlp never walks the continuation tokens past the first batch, so we
    always ask for 1:end and slice locally."""
    cached = SCRAPE_CACHE.get(url)
    if cached and cached["end"] >= end:
        return cached["entries"]
    entries = ytdlp.run_json_lines(
        ["--flat-playlist", "--dump-json", "--no-warnings", "--ignore-no-formats-error",
         "--playlist-items", f"1:{end}", url],
        timeout=config.SCRAPE_TIMEOUT, what="scrape")
    SCRAPE_CACHE.set(url, {"entries": entries, "end": end})
    return entries


def _videos(entries: list[dict[str, Any]], ignore_shorts: bool):
    """(videos, channel name, skipped shorts) for a run of entries."""
    videos: list[dict[str, Any]] = []
    channel: str | None = None
    skipped = 0
    for entry in entries:
        v = entry_to_video(entry)
        if v is None:
            continue
        channel = channel or v.get("channel")
        if ignore_shorts and is_short(v["url"]):
            skipped += 1
            continue
        videos.append(v)
    return videos, channel, skipped


def scrape_channel(url: str, page: int, page_size: int, ignore_shorts: bool = True,
                   filters: dict[str, Any] | None = None, sort: str = "newest") -> dict[str, Any]:
    """One page of videos from a channel/playlist URL.

    Unfiltered, this pages a sliding window of the channel (fast). With any
    filter or a non-default sort it pulls the whole channel (capped) and pages
    the filtered+sorted result, so search/sort span the entire channel."""
    if not ytdlp.available():
        raise RuntimeError("yt-dlp is not installed or not on PATH")
    filters = filters or {}
    sort = sort if sort in config.SORT_ORDERS else "newest"
    common = {"page_size": page_size, "ignore_shorts": ignore_shorts}

    if not filters_active(filters, sort):
        start, end = (page - 1) * page_size, page * page_size
        window = fetch_entries(url, end)[start:end]
        videos, channel, skipped = _videos(window, ignore_shorts)
        # page_entries is the raw window size: the UI pages on it, because a
        # window full of hidden Shorts still has more pages behind it.
        return {**common, "channel": channel, "videos": videos, "page": page,
                "count": len(videos), "page_entries": len(window),
                "skipped_shorts": skipped, "filtered": False}

    entries = fetch_entries(url, config.SCRAPE_ALL_CAP)
    videos, channel, skipped = _videos(entries, ignore_shorts)
    matched = sort_videos([v for v in videos if passes_filters(v, filters)], sort)
    total = len(matched)
    total_pages = max(1, -(-total // page_size))
    page = max(1, min(page, total_pages))
    window = matched[(page - 1) * page_size: page * page_size]
    return {**common, "channel": channel, "videos": window, "page": page,
            "count": len(window), "total": total, "total_pages": total_pages,
            "scanned": len(entries), "capped": len(entries) >= config.SCRAPE_ALL_CAP,
            "skipped_shorts": skipped, "filtered": True}


def validate_source(text: str) -> tuple[str, str]:
    """Classify the search box: ('url', normalised URL) or ('query', text).
    Raises ValueError for input that is clearly unusable."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Enter a channel URL, @handle, or a name to search")
    if text.startswith("@") and " " not in text:
        return "url", f"https://www.youtube.com/{text}"
    has_scheme = "://" in text
    parsed = urlparse(text if has_scheme else "https://" + text)
    if has_scheme or ("." in (parsed.netloc or "") and " " not in text):
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported URL scheme: {parsed.scheme or 'none'}")
        if not parsed.netloc:
            raise ValueError("That does not look like a valid URL")
        return "url", text if has_scheme else "https://" + text
    return "query", text


def search_channels(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Candidate channels for a plain-text name. yt-dlp has no channel search,
    so we mine the channel fields off a small `ytsearch` of videos."""
    entries = ytdlp.run_json_lines(
        ["--flat-playlist", "--dump-json", "--no-warnings", "--ignore-no-formats-error",
         f"ytsearch{max(limit * 4, 12)}:{query}"],
        timeout=60, what="search")
    seen: dict[str, dict[str, Any]] = {}
    for e in entries:
        name = e.get("channel") or e.get("uploader")
        curl = e.get("channel_url") or e.get("uploader_url")
        cid = e.get("channel_id") or curl or name
        if not name or not curl or cid in seen:
            continue
        seen[cid] = {"name": name, "url": curl, "channel_id": e.get("channel_id")}
        if len(seen) >= limit:
            break
    return list(seen.values())
