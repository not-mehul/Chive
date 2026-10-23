"""Proper tags, cover art and chapter-based album splitting, via ffmpeg.

yt-dlp's own chapter splitter copies the whole album's tags onto every track,
so Chive splits albums itself: one ffmpeg pass per chapter that cuts the
track, writes *its* tags (title, track n/N, album, artist, year…) and embeds
square cover art. The same tagger gives podcast episodes real tags from the
feed (show, author, date, artwork) instead of the generic extractor's guesses.

Runs inside ytdlp_runner.py (a subprocess), so it only imports the stdlib and
`chive.library`. The argv builders are pure and unit-tested; the runners shell
out to ffmpeg.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .library import safe_segment

# Containers ffmpeg can embed a cover picture into, as an attached picture.
_COVER_CONTAINERS = {".mp3", ".m4a", ".mp4", ".m4b", ".flac"}
_AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav"}

# Noise that music uploads wrap around the real album/track name.
_TITLE_NOISE = re.compile(
    r"\s*[\(\[【](?:full\s+album|official\s+(?:audio|video|music\s+video|lyric\s+video)|"
    r"audio|lyrics?|hd|hq|4k|visuali[sz]er|album\s+stream)[\)\]】]\s*|\s+-\s+full\s+album\s*$|"
    r"\s+full\s+album\s*$",
    re.IGNORECASE)
# Track numbering only when clearly delimited — "01. ", "3 - ", "[04] ", "(2) " —
# so titles that merely start with a number ("1979", "99 Luftballons") survive.
_LEADING_NUMBER = re.compile(
    r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\s*[.):]|\d{1,3}\s+[-–—])\s*(?=\S)")
_SEPARATORS = (" - ", " – ", " — ", " | ")

# Tag keys → ffmpeg metadata keys.
_TAG_KEYS = (("title", "title"), ("artist", "artist"), ("album", "album"),
             ("album_artist", "album_artist"), ("date", "date"), ("genre", "genre"),
             ("comment", "comment"), ("disc", "disc"))


# ----- metadata heuristics ---------------------------------------------------


def clean_artist(name: str | None) -> str:
    """'Daft Punk - Topic' / 'DaftPunkVEVO' → 'Daft Punk' / 'DaftPunk'."""
    s = (name or "").strip()
    s = re.sub(r"\s*-\s*Topic$", "", s)
    s = re.sub(r"VEVO$", "", s).strip()
    return s


def strip_noise(title: str | None) -> str:
    s = title or ""
    prev = None
    while prev != s:
        prev = s
        s = _TITLE_NOISE.sub(" ", s).strip()
    return re.sub(r"\s{2,}", " ", s).strip(" -–—|")


def guess_artist_album(title: str | None, uploader: str | None,
                       artist: str | None = None, album: str | None = None) -> tuple[str, str]:
    """Best-effort (artist, album) for a full-album upload.

    Explicit metadata wins (YouTube Music and Bandcamp provide it); otherwise
    "Artist - Album (Full Album)" titles are split, falling back to the
    uploader as artist and the cleaned title as album."""
    clean = strip_noise(title)
    if artist and album:
        return clean_artist(artist), strip_noise(album)
    for sep in _SEPARATORS:
        if sep in clean:
            a, b = clean.split(sep, 1)
            if a.strip() and b.strip():
                return clean_artist(artist) or a.strip(), strip_noise(album) or b.strip()
    return (clean_artist(artist) or clean_artist(uploader) or "Unknown Artist",
            strip_noise(album) or clean or "Unknown Album")


def clean_track_title(title: str | None, artist: str | None = None) -> str:
    """'03. Artist - Song (Official Audio)' → 'Song'."""
    s = strip_noise(title)
    s = _LEADING_NUMBER.sub("", s, count=1)
    if artist:
        for sep in _SEPARATORS:
            prefix = artist + sep
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):]
                break
    return s.strip() or (title or "Untitled").strip()


def track_filename(number: int, title: str, ext: str) -> str:
    return f"{number:02d} - {safe_segment(title)}{ext}"


# ----- ffmpeg argv builders (pure) -------------------------------------------


def metadata_args(tags: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for key, ff in _TAG_KEYS:
        value = tags.get(key)
        if value not in (None, ""):
            args += ["-metadata", f"{ff}={value}"]
    track = tags.get("track")
    if track:
        total = tags.get("track_total")
        args += ["-metadata", f"track={track}/{total}" if total else f"track={track}"]
    return args


def tag_command(ffmpeg: str, src: Path, dst: Path, tags: dict[str, Any],
                cover: Path | None = None, start: float | None = None,
                duration: float | None = None) -> list[str]:
    """ffmpeg argv that stream-copies `src` (or a slice of it) to `dst` with
    fresh tags and, where the container allows, an embedded cover."""
    ext = dst.suffix.lower()
    audio = ext in _AUDIO_EXTS
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(src)]
    with_cover = bool(cover) and audio and ext in _COVER_CONTAINERS
    if with_cover:
        cmd += ["-i", str(cover)]
    # Audio files: just the audio (any old cover stream is replaced). Others
    # (video podcasts): keep every stream.
    cmd += ["-map", "0:a"] if audio else ["-map", "0"]
    if with_cover:
        cmd += ["-map", "1:v", "-disposition:v:0", "attached_pic"]
    cmd += ["-c", "copy", "-map_metadata", "-1"]
    # A track cut from an album must not inherit the album's chapter list.
    cmd += ["-map_chapters", "-1"] if start is not None else []
    if ext == ".mp3":
        cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]
        if with_cover:
            cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    return cmd + metadata_args(tags) + [str(dst)]


def cover_command(ffmpeg: str, src: Path, dst: Path, square: bool) -> list[str]:
    """Convert any image (webp, png…) to a JPEG cover; optionally centre-crop
    to a square, since video thumbnails are 16:9 with letterboxing."""
    filters = ["crop='min(iw,ih)':'min(iw,ih)'"] if square else []
    filters.append("scale='min(1200,iw)':-2")
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-vf", ",".join(filters), "-frames:v", "1", "-q:v", "2", str(dst)]


def chapter_spans(chapters: list[dict[str, Any]], total: float | None) -> list[tuple[float, float, str]]:
    """(start, duration, title) for each usable chapter, in order."""
    spans = []
    ordered = sorted((c for c in chapters if c.get("start_time") is not None),
                     key=lambda c: c["start_time"])
    for i, ch in enumerate(ordered):
        start = float(ch["start_time"])
        end = ch.get("end_time")
        if end is None:
            end = ordered[i + 1]["start_time"] if i + 1 < len(ordered) else total
        if end is None or float(end) <= start:
            continue
        spans.append((start, float(end) - start, ch.get("title") or f"Track {i + 1}"))
    return spans


# ----- running ffmpeg ----------------------------------------------------------


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"ffmpeg exited {proc.returncode}")


def _download(url: str, dest: Path, timeout: int = 30) -> None:
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("cover URL must be http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Chive)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def prepare_cover(ffmpeg: str, source: str | None, dest: Path, square: bool) -> Path | None:
    """A JPEG cover at `dest` from a URL or local image; reuses an existing
    `dest`. Returns None (never raises) if no cover could be made."""
    if dest.is_file() and dest.stat().st_size:
        return dest
    if not source:
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="chive-cover-"))
    try:
        src = Path(source)
        if not src.is_file():
            src = tmp_dir / "source"
            _download(source, src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(cover_command(ffmpeg, src, dest, square))
        return dest if dest.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def tag_file(ffmpeg: str, path: Path, tags: dict[str, Any], cover: Path | None) -> None:
    """Rewrite `path` in place with fresh tags (via a temp file + replace)."""
    tmp = path.with_name(f".{path.stem}.chive-tagging{path.suffix}")
    try:
        _run(tag_command(ffmpeg, path, tmp, tags, cover))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def split_album(ffmpeg: str, src: Path, spans: list[tuple[float, float, str]],
                tags: dict[str, Any], cover: Path | None, out_dir: Path,
                on_track=None) -> list[dict[str, Any]]:
    """Cut `src` into one tagged file per span. Returns [{path, title,
    track, duration}] for the files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(spans)
    written = []
    for n, (start, dur, raw_title) in enumerate(spans, 1):
        title = clean_track_title(raw_title, tags.get("artist"))
        dst = out_dir / track_filename(n, title, src.suffix)
        if on_track:
            on_track(n, total, title)
        track_tags = {**tags, "title": title, "track": n, "track_total": total}
        _run(tag_command(ffmpeg, src, dst, track_tags, cover, start=start, duration=dur))
        written.append({"path": str(dst), "title": title, "track": n, "duration": round(dur)})
    return written
