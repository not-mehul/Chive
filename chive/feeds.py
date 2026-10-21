"""Podcast feeds out of the archive, and OPML for followed shows.

Every archived show gets a local RSS feed (`/feeds/<show>.xml`) whose
enclosures point back at Chive, so any podcast app on this machine (or the
LAN, if you expose Chive there) can play your archive.
"""

from __future__ import annotations

import calendar
import mimetypes
import time
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from .catalog import AUDIO_EXTS, COVER_NAMES, LibraryIndex, guess_source, scan

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def _episodes_by_show(root: Path, index: LibraryIndex) -> dict[str, list[tuple[dict, dict]]]:
    """Show folder → [(scan record, index entry)] for archived podcast episodes."""
    entries = index.snapshot()
    shows: dict[str, list[tuple[dict, dict]]] = {}
    for f in scan(root):
        rel = f["rel"]
        if "/" not in rel or Path(rel).suffix.lower() not in AUDIO_EXTS | {".mp4", ".m4v"}:
            continue
        e = entries.get(rel) or {}
        if (e.get("source") or guess_source(rel)) != "podcast":
            continue
        shows.setdefault(rel.split("/")[0], []).append((f, e))
    return shows


def archived_shows(root: Path, index: LibraryIndex) -> list[dict[str, Any]]:
    """[{name, slug, episodes, cover}] for every show with archived episodes."""
    out = []
    for folder, eps in sorted(_episodes_by_show(root, index).items(), key=lambda kv: kv[0].lower()):
        cover = next((f"{folder}/{n}" for n in COVER_NAMES if (root / folder / n).is_file()), None)
        out.append({"name": folder, "slug": quote(folder, safe=""), "episodes": len(eps), "cover": cover})
    return out


def _file_url(base_url: str, rel: str) -> str:
    return f"{base_url}api/library/file?path={quote(rel, safe='')}"


def _hms(seconds: Any) -> str | None:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _pubdate(entry: dict, mtime: float) -> str:
    date = (entry.get("date") or "").replace("-", "")
    if len(date) == 8 and date.isdigit():
        try:
            ts = calendar.timegm(time.strptime(date, "%Y%m%d"))
            return formatdate(ts, usegmt=True)
        except ValueError:
            pass
    return formatdate(mtime, usegmt=True)


def podcast_rss(root: Path, folder: str, index: LibraryIndex, base_url: str) -> str | None:
    """RSS 2.0 for one archived show, newest first; None if it has no episodes."""
    eps = _episodes_by_show(root, index).get(folder)
    if not eps:
        return None
    eps.sort(key=lambda fe: (fe[1].get("date") or "", fe[0]["mtime"]), reverse=True)
    author = next((e.get("artist") for _, e in eps if e.get("artist")), None)
    cover = next((f"{folder}/{n}" for n in COVER_NAMES if (root / folder / n).is_file()), None)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<rss version="2.0" xmlns:itunes="{_ITUNES}">', "<channel>",
           f"<title>{escape(folder)}</title>",
           f"<link>{escape(base_url)}</link>",
           f"<description>{escape(f'{folder} — archived with Chive')}</description>",
           "<generator>Chive</generator>"]
    if author:
        out.append(f"<itunes:author>{escape(author)}</itunes:author>")
    if cover:
        url = _file_url(base_url, cover)
        out += [f"<image><url>{escape(url)}</url><title>{escape(folder)}</title>"
                f"<link>{escape(base_url)}</link></image>",
                f"<itunes:image href={quoteattr(url)}/>"]
    for f, e in eps:
        rel = f["rel"]
        mime = mimetypes.guess_type(rel)[0] or "audio/mpeg"
        out += ["<item>",
                f"<title>{escape(e.get('title') or Path(rel).stem)}</title>",
                f'<guid isPermaLink="false">{escape(e.get("video_id") or rel)}</guid>',
                f"<pubDate>{_pubdate(e, f['mtime'])}</pubDate>",
                f"<enclosure url={quoteattr(_file_url(base_url, rel))} length=\"{f['size']}\" "
                f"type={quoteattr(mime)}/>"]
        dur = _hms(e.get("duration"))
        if dur:
            out.append(f"<itunes:duration>{dur}</itunes:duration>")
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out) + "\n"


# ----- OPML --------------------------------------------------------------------


def opml_export(follows: list[dict[str, Any]]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">',
             "<head><title>Chive — followed podcasts</title></head>", "<body>"]
    for f in follows:
        name = f.get("name") or f["feed_url"]
        lines.append(f'<outline type="rss" text={quoteattr(name)} title={quoteattr(name)} '
                     f'xmlUrl={quoteattr(f["feed_url"])}/>')
    lines += ["</body>", "</opml>"]
    return "\n".join(lines) + "\n"


def opml_import(text: str) -> list[dict[str, Any]]:
    """[{feed_url, name}] from an OPML document (nested outlines included).
    Raises ValueError for something that isn't OPML."""
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("OPML with a DOCTYPE/ENTITY declaration is not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"not valid OPML: {exc}") from None
    if root.tag.lower() != "opml":
        raise ValueError("not an OPML document")
    shows: dict[str, dict[str, Any]] = {}
    for node in root.iter("outline"):
        url = (node.get("xmlUrl") or node.get("xmlurl") or "").strip()
        if url.startswith(("http://", "https://")) and url not in shows:
            shows[url] = {"feed_url": url, "name": node.get("title") or node.get("text") or url}
    return list(shows.values())
