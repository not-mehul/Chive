"""What is already archived on disk, and how archived files are named.

YouTube files embed the 11-char video id as "[<id>]" (our output template).
Podcast files are "<title> [<tag>].<ext>", the tag derived from the episode's
guid so same-titled episodes ("Trailer", "Bonus") never share a file.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

_IN_PROGRESS_SUFFIXES = (".part", ".ytdl", ".temp")
VIDEO_ID_RE = re.compile(r"\[([0-9A-Za-z_-]{11})\]")
POD_TAG_RE = re.compile(r"\[([0-9a-f]{8})\]$")
# Unsafe on some filesystem, plus `%` (yt-dlp output-template syntax).
_FS_BAD = re.compile(r'[<>:"/\\|?*%\x00-\x1f]')


def safe_segment(name: str) -> str:
    """A filesystem-safe path segment, also safe as literal text in a yt-dlp
    output template."""
    s = _FS_BAD.sub("", name or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s[:120] or "untitled"


def pod_tag(guid: str) -> str:
    """Short stable tag for a podcast episode, embedded in its filename."""
    return hashlib.sha1((guid or "").encode("utf-8")).hexdigest()[:8]


def podcast_filename(title: str, guid: str, ext: str) -> str:
    return f"{safe_segment(title)} [{pod_tag(guid)}].{ext}"


def _finished_files(folder: Path, recursive: bool):
    try:
        for p in (folder.rglob("*") if recursive else folder.iterdir()):
            # Skip in-progress artifacts: a half-finished download is not archived.
            if p.suffix.lower() in _IN_PROGRESS_SUFFIXES:
                continue
            if p.is_file():
                yield p
    except OSError:
        return


def scan_video_ids(folder: Path) -> set[str]:
    """YouTube ids of finished downloads anywhere under `folder`."""
    ids: set[str] = set()
    for p in _finished_files(folder, recursive=True):
        m = VIDEO_ID_RE.search(p.name)
        if m:
            ids.add(m.group(1))
    return ids


def archive_ids(folder: Path) -> set[str]:
    """Ids recorded in yt-dlp's download archive (`<folder>/archive.txt`)."""
    path = folder / "archive.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return {tok[-1] for tok in (ln.split() for ln in text.splitlines()) if tok}


def podcast_downloaded(folder: Path, episodes: list[dict[str, Any]]) -> set[str]:
    """Guids of `episodes` with a finished file in the show's `folder`.
    Files from before the tag existed ("<title>.<ext>") still match by title."""
    tags: set[str] = set()
    stems: set[str] = set()
    for p in _finished_files(folder, recursive=False):
        stems.add(p.stem)
        m = POD_TAG_RE.search(p.stem)
        if m:
            tags.add(m.group(1))
    have: set[str] = set()
    for ep in episodes:
        guid = ep.get("video_id") or ""
        if pod_tag(guid) in tags or safe_segment(ep.get("title") or "") in stems:
            have.add(guid)
    return have
