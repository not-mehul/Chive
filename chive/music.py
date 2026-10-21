"""Music: resolve albums/tracks and build their tags.

Three ways in:
  * a full-album upload (one video with chapters) → queued as an *album*,
    split into one tagged track per chapter;
  * a playlist / album URL (YouTube Music, Bandcamp, SoundCloud…) → each
    entry queued as a *track* with its album position;
  * plain text → a YouTube search for "<text> full album".
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from . import config, ytdlp
from .tagging import clean_artist, clean_track_title, guess_artist_album, strip_noise

MODES = ("album", "track")


def _is_url(text: str) -> bool:
    p = urlparse(text)
    return p.scheme in ("http", "https") and bool(p.netloc)


def _thumb(e: dict[str, Any]) -> str | None:
    thumbs = e.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url")
    return e.get("thumbnail")


def _year(e: dict[str, Any]) -> str | None:
    if e.get("release_year"):
        return str(e["release_year"])
    d = e.get("release_date") or e.get("upload_date") or ""
    return d[:4] if len(d) >= 4 else None


def _album_card(e: dict[str, Any]) -> dict[str, Any]:
    """A single video: a full album when it has chapters, else one track."""
    artist, album = guess_artist_album(e.get("title"), e.get("channel") or e.get("uploader"),
                                       e.get("artist") or e.get("creator"), e.get("album"))
    chapters = len(e.get("chapters") or [])
    return {
        "video_id": e.get("id"), "url": e.get("webpage_url") or e.get("url"),
        "title": e.get("title") or e.get("id"), "thumbnail": _thumb(e),
        "duration": e.get("duration"), "artist": artist, "album": album, "year": _year(e),
        "chapters": chapters, "music_mode": "album" if chapters >= 2 else "track",
    }


def _playlist_album(entries: list[dict[str, Any]]) -> tuple[str, str]:
    first = entries[0]
    album = strip_noise(first.get("playlist_title") or first.get("playlist") or "Unknown Album")
    if album.lower().startswith("album - "):          # YouTube Music naming
        album = album[8:]
    artist = clean_artist(first.get("playlist_uploader") or first.get("playlist_channel")
                          or first.get("artist") or first.get("channel") or first.get("uploader"))
    return artist or "Unknown Artist", album


def resolve(text: str) -> dict[str, Any]:
    """Cards for a music URL or search text. Raises ValueError/RuntimeError."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Paste an album or playlist URL, or type an album to search")
    if not _is_url(text):
        entries = ytdlp.run_json_lines(
            ["--flat-playlist", "--dump-json", "--no-warnings", f"ytsearch20:{text} full album"],
            timeout=60, what="music search")
        cards = [_album_card(e) for e in entries if e.get("id")]
        for c in cards:              # search hits: assume full albums; split if chapters exist
            c["music_mode"] = "album"
            c["chapters"] = None
        return {"kind": "search", "videos": cards}

    entries = ytdlp.run_json_lines(
        ["--flat-playlist", "--dump-json", "--no-warnings", "--ignore-no-formats-error",
         "--playlist-items", f"1:{config.RESOLVE_LIMIT}", text],
        timeout=config.SCRAPE_TIMEOUT, what="music lookup")
    entries = [e for e in entries if e.get("id")]
    if not entries:
        raise RuntimeError("nothing playable found at that URL")
    is_playlist = len(entries) > 1 or entries[0].get("_type") == "url" or entries[0].get("playlist_title")
    if not is_playlist:
        return {"kind": "single", "videos": [_album_card(entries[0])]}

    artist, album = _playlist_album(entries)
    total = entries[0].get("playlist_count") or len(entries)
    cards = []
    for i, e in enumerate(entries, 1):
        track_artist = clean_artist(e.get("artist") or e.get("creator")) or artist
        cards.append({
            "video_id": e.get("id"), "url": e.get("url") or e.get("webpage_url"),
            "title": clean_track_title(e.get("track") or e.get("title"), track_artist),
            "thumbnail": _thumb(e), "duration": e.get("duration"),
            "artist": track_artist, "album": album, "album_artist": artist, "year": _year(e),
            "track": e.get("playlist_index") or i, "track_total": total, "music_mode": "track",
        })
    return {"kind": "playlist", "album": album, "artist": artist, "videos": cards}


def _text(v: Any, limit: int = 300) -> str | None:
    return str(v).strip()[:limit] if v not in (None, "") else None


def _int(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def item_extra(v: dict[str, Any]) -> dict[str, Any]:
    """Validated music metadata for a queued card → QueueItem.extra."""
    mode = v.get("music_mode") if v.get("music_mode") in MODES else "album"
    artist = _text(v.get("artist")) or "Unknown Artist"
    album = _text(v.get("album")) or _text(v.get("title")) or "Unknown Album"
    tags = {
        "artist": artist, "album": album,
        "album_artist": _text(v.get("album_artist")) or artist,
        "date": _text(v.get("year"), 10),
    }
    if mode == "track":
        tags["title"] = _text(v.get("title"))
        tags["track"] = _int(v.get("track"))
        tags["track_total"] = _int(v.get("track_total"))
    return {"music_mode": mode, "tags": {k: val for k, val in tags.items() if val is not None}}
