"""Generic URL mode: anything yt-dlp supports (Vimeo, Twitch VODs,
SoundCloud, news sites, …) — a single video or a playlist of them."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from . import config, ytdlp


def require_url(text: str) -> str:
    """An http(s) URL; a bare "site.com/path" gets https:// added."""
    url = (text or "").strip()
    explicit = "://" in url
    p = urlparse(url if explicit else "https://" + url)
    looks_like_host = explicit or ("." in (p.netloc or "") and " " not in url)
    if p.scheme not in ("http", "https") or not p.netloc or not looks_like_host:
        raise ValueError("Paste a link (http/https) to a video or playlist")
    return url if explicit else "https://" + url


def _card(e: dict[str, Any], page_url: str, single: bool) -> dict[str, Any]:
    thumbs = e.get("thumbnails") or []
    thumb = thumbs[-1].get("url") if isinstance(thumbs, list) and thumbs else e.get("thumbnail")
    url = (e.get("webpage_url") or page_url) if single else (e.get("url") or e.get("webpage_url"))
    return {
        "video_id": str(e.get("id")), "url": url, "title": e.get("title") or str(e.get("id")),
        "thumbnail": thumb, "duration": e.get("duration"), "upload_date": e.get("upload_date"),
        "view_count": e.get("view_count"),
        "site": e.get("extractor_key") or e.get("ie_key") or e.get("extractor"),
        "channel": e.get("uploader") or e.get("channel"),
    }


def resolve(text: str) -> dict[str, Any]:
    """Cards for any supported URL. Raises ValueError/RuntimeError."""
    url = require_url(text)
    entries = ytdlp.run_json_lines(
        ["--flat-playlist", "--dump-json", "--no-warnings", "--ignore-no-formats-error",
         "--playlist-items", f"1:{config.RESOLVE_LIMIT}", url],
        timeout=config.SCRAPE_TIMEOUT, what="lookup")
    entries = [e for e in entries if e.get("id") is not None]
    if not entries:
        raise RuntimeError("yt-dlp found nothing to download at that URL")
    single = len(entries) == 1 and entries[0].get("_type") != "url"
    videos = [_card(e, url, single) for e in entries if (e.get("url") or e.get("webpage_url") or single)]
    title = entries[0].get("playlist_title") or (videos[0]["title"] if single else None)
    return {"kind": "single" if single else "playlist", "title": title,
            "site": videos[0].get("site") if videos else None, "videos": videos}
