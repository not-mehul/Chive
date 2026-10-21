"""Turn yt-dlp output lines into item progress, phase messages and outcomes.

Pure functions over a QueueItem — the caller holds whatever lock guards it.

Two machine-readable lines matter (from ytdlp_runner.py's flushing hooks, or
from --progress-template in the binary fallback):
    [YTPROG] downloaded|total|estimate|speed|eta|status|ext
    [YTPP]   <postprocessor name>
Everything else is yt-dlp's normal status output, used for phases, the final
file path, errors, and archive/already-downloaded detection.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import QueueItem
from .util import fmt_eta, human_bytes

# --progress-template for the binary fallback: the same [YTPROG] fields.
PROGRESS_TEMPLATE = (
    "download:[YTPROG] %(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
    "%(progress.status)s|%(info.ext)s"
)

# What apply_line() reports about a line.
PROGRESS, POSTPROCESS, ARCHIVED, ON_DISK, OTHER = (
    "progress", "postprocess", "archived", "on_disk", "other")

AGE_RESTRICTED = "Age-restricted — add cookies under Settings → Access & throughput"

_FILEPATH_RE = re.compile(r"^filepath:(.+)$")
_ERROR_RE = re.compile(r"^ERROR:\s*(.+)$", re.IGNORECASE)
_DESTINATION_RE = re.compile(r"^\[download\]\s+Destination:\s*(.+)$")

_AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".aac", ".wav", ".flac", ".ogg"}
_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi", ".ts"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Status-line markers → phase shown on the row (for the binary fallback, and
# as a backstop where no [YTPP] line is emitted).
_PHASE_MARKERS = (
    ("[SponsorBlock]", "Querying SponsorBlock"),
    ("[Merger]", "Merging streams"),
    ("[ExtractAudio]", "Extracting audio"),
    ("[EmbedSubtitle]", "Embedding subtitles"),
    ("[EmbedThumbnail]", "Embedding thumbnail"),
    ("[Metadata]", "Writing metadata"),
    ("[ModifyChapters]", "Cutting segments"),
)

# Phases with no meaningful percentage — the UI shows an indeterminate bar.
INDETERMINATE_PHASES = frozenset({
    "Querying SponsorBlock", "Merging streams", "Extracting audio",
    "Embedding thumbnail", "Embedding subtitles", "Writing metadata",
    "Writing chapters", "Cutting segments", "Processing",
})


def _ext_kind(ext: str | None) -> str:
    if not ext or ext in ("NA", "none"):
        return "Downloading"
    e = ext.lower()
    if not e.startswith("."):
        e = "." + e
    if e in _AUDIO_EXTS:
        return "Downloading audio"
    if e in _VIDEO_EXTS:
        return "Downloading video"
    if e in _IMAGE_EXTS:
        return "Downloading thumbnail"
    return "Downloading"


def _num(v: str) -> float | None:
    v = (v or "").strip()
    if not v or v in ("NA", "N/A", "None", "none"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def pp_phase(name: str) -> str:
    """Postprocessor class name → the phase shown to the user."""
    n = (name or "").lower()
    for needle, phase in (
        ("merger", "Merging streams"),
        ("modifychapters", "Cutting segments"),
        ("sponsorblock", "Cutting segments"),
        ("extractaudio", "Extracting audio"),
        ("embedsubtitle", "Embedding subtitles"),
        ("subtitlesconvert", "Embedding subtitles"),
        ("embedthumbnail", "Embedding thumbnail"),
        ("metadata", "Writing metadata"),
        ("embedchapter", "Writing chapters"),
    ):
        if needle in n:
            return phase
    return "Processing"


def looks_age_restricted(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in (
        "sign in to confirm your age", "age-restricted",
        "inappropriate for some users", "confirm you're not a bot",
    ))


def _apply_progress(item: QueueItem, line: str) -> None:
    parts = [p.strip() for p in line.split("[YTPROG]", 1)[1].strip().split("|")]
    if len(parts) < 7:
        return
    downloaded = _num(parts[0])
    total = _num(parts[1]) or _num(parts[2])
    speed_bps = _num(parts[3])
    status, ext = parts[5], parts[6]
    if total and downloaded is not None:
        item.total_bytes = total
        item.downloaded_bytes = downloaded
        item.progress = max(0.0, min(100.0, downloaded / total * 100.0))
    elif downloaded is not None:
        item.downloaded_bytes = downloaded
    item.speed_bps = speed_bps
    item.speed = (human_bytes(speed_bps) + "/s") if speed_bps else None
    item.eta = fmt_eta(_num(parts[4]))
    if status == "finished" and item.total_bytes:
        item.progress = 100.0
    item.message = _ext_kind(ext)


def apply_line(item: QueueItem, line: str) -> str:
    """Update `item` from one output line; returns what kind of line it was."""
    line = line.rstrip("\r")
    if line.startswith("[YTPP]"):
        item.message = pp_phase(line[6:].strip())
        return POSTPROCESS
    if "[YTPROG]" in line:
        _apply_progress(item, line)
        return PROGRESS
    if "has already been recorded in the archive" in line:
        return ARCHIVED
    if "has already been downloaded" in line:
        return ON_DISK
    dest = _DESTINATION_RE.match(line)
    if dest:
        # A new stream/file begins. The bar is NOT reset: with the API runner
        # progress is already aggregated across the video+audio streams.
        item.message = _ext_kind(Path(dest.group(1).strip()).suffix)
        item.reset_transfer()
        return OTHER
    fp = _FILEPATH_RE.match(line)
    if fp:
        item.output_file = fp.group(1).strip()
        return OTHER
    for marker, phase in _PHASE_MARKERS:
        if marker in line:
            item.message = phase
            return OTHER
    err = _ERROR_RE.match(line)
    if err:
        msg = err.group(1)
        item.message = (AGE_RESTRICTED if looks_age_restricted(msg) else msg)[:200]
        return OTHER
    if looks_age_restricted(line):
        item.message = AGE_RESTRICTED
    return OTHER


def summary(item: QueueItem) -> str:
    """A readable progress line for the item log."""
    parts = [f"[progress] {item.progress:5.1f}%"]
    if item.total_bytes:
        parts.append(f"of {human_bytes(item.total_bytes)}")
    if item.speed:
        parts.append(f"at {item.speed}")
    if item.eta:
        parts.append(f"eta {item.eta}")
    return " ".join(parts)
