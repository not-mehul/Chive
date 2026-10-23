"""Environment, paths and fixed constants.

Everything here is read once at import. Environment variables use the
`CHIVE_` prefix, with the pre-rename `YTARCHIVE_` names honoured as fallbacks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
RUNNER_PATH = Path(__file__).resolve().parent / "ytdlp_runner.py"

IS_WINDOWS = os.name == "nt"


def env(name: str) -> str | None:
    """Read CHIVE_<name>, falling back to the legacy YTARCHIVE_<name>."""
    return os.environ.get(f"CHIVE_{name}") or os.environ.get(f"YTARCHIVE_{name}")


def env_truthy(name: str) -> bool:
    return (env(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def default_state_dir() -> Path:
    """~/.chive by default; keep using an existing ~/.ytarchive from an earlier
    install so upgrades don't orphan a user's queue and settings."""
    override = env("STATE_DIR")
    if override:
        return Path(override).expanduser()
    chive = Path.home() / ".chive"
    legacy = Path.home() / ".ytarchive"
    return legacy if (legacy.exists() and not chive.exists()) else chive


def default_download_dir() -> Path:
    """Where downloads go until the user picks a folder.

    Deliberately *outside* the app folder (~/Downloads/Chive), so deleting or
    re-cloning the app can't take the library with it. An existing, non-empty
    `downloads/` inside the app folder (the pre-1.1 default) is kept in use.
    """
    override = env("DOWNLOAD_DIR")
    if override:
        return Path(override).expanduser()
    legacy = ROOT / "downloads"
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
    except OSError:
        pass
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / "Chive"


STATE_DIR = default_state_dir()
STATE_FILE = STATE_DIR / "state.json"
STATE_VERSION = 1

# With CHIVE_DEBUG set, every download's raw yt-dlp output is also written —
# with per-line wall-clock offsets — to STATE_DIR/logs for troubleshooting.
DEBUG = env_truthy("DEBUG")
DEBUG_LOG_DIR = STATE_DIR / "logs"
# Per-item download logs (what the queue's log button shows). Written as lines
# arrive so they survive a restart; deleted when the item is removed.
ITEM_LOG_DIR = STATE_DIR / "item-logs"
LOG_MAX_LINES = 2000

HISTORY_MAX = 10
MAX_CONCURRENCY = 4

# Categories yt-dlp can *remove* via --sponsorblock-remove, with display labels
# (the UI renders exactly this list, in this order). `poi_highlight` is a point
# marker, not a removable span, so it is deliberately excluded.
SPONSORBLOCK_LABELS: dict[str, str] = {
    "sponsor": "Sponsor",
    "selfpromo": "Self-promotion",
    "interaction": "Interaction prompt",
    "intro": "Intro",
    "outro": "Outro",
    "preview": "Preview / recap",
    "filler": "Filler",
    "music_offtopic": "Off-topic music",
}
SPONSORBLOCK_CATEGORIES = frozenset(SPONSORBLOCK_LABELS)

# Remove (cut segments out) is the primary behaviour; mark leaves the file
# intact and labels the segments as chapters for skip-aware players.
SPONSORBLOCK_MODES = ("remove", "mark")

# Ordered: the UI's quality control lists presets in this order.
QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "4k": {
        "label": "4K",
        "hint": "Up to 2160p MP4 — large files, sharpest fidelity.",
        "format": "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/bv*[height<=2160]+ba/b[height<=2160]",
        "merge_output_format": "mp4",
        "audio_only": False,
    },
    "1080p": {
        "label": "1080p",
        "hint": "1080p MP4 — the default. Balance of fidelity and disk.",
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]",
        "merge_output_format": "mp4",
        "audio_only": False,
    },
    "720p": {
        "label": "720p",
        "hint": "720p MP4 — modest file size, still legible on TV.",
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]",
        "merge_output_format": "mp4",
        "audio_only": False,
    },
    "m4a": {
        "label": "M4A",
        "hint": "M4A audio container, AAC stream. No transcode where possible.",
        "format": "ba[ext=m4a]/ba",
        "audio_only": True,
        "audio_format": "m4a",
    },
    "mp3": {
        "label": "MP3",
        "hint": "MP3 audio, 192kbps default. Requires ffmpeg transcoding.",
        "format": "ba/b",
        "audio_only": True,
        "audio_format": "mp3",
    },
}
DEFAULT_QUALITY = "1080p"
# Podcast episodes are downloaded as published; they carry no quality preset.
PODCAST_QUALITY = "original"

# Music: best audio, extracted to a taggable container (both carry cover art).
MUSIC_FORMATS: dict[str, dict[str, Any]] = {
    "mp3": {"label": "MP3", "format": "ba/b", "audio_format": "mp3",
            "hint": "MP3 (VBR, best quality) — plays everywhere."},
    "m4a": {"label": "M4A", "format": "ba[ext=m4a]/ba/b", "audio_format": "m4a",
            "hint": "M4A / AAC — usually no re-encode from YouTube."},
}
DEFAULT_MUSIC_FORMAT = "mp3"

# Top-level folders (inside the download folder) for the newer sources.
# YouTube and podcasts keep their original layout for compatibility.
MUSIC_DIR = "Music"
WEB_DIR = "Web"

# Podcast auto-sync choices, in hours (0 = off).
PODCAST_SYNC_CHOICES = (0, 1, 6, 12, 24)
RESOLVE_LIMIT = 300         # entries listed for a music/web URL

# yt-dlp's accepted --cookies-from-browser values, offered as a closed list.
COOKIE_BROWSERS = (
    "brave", "chrome", "chromium", "edge", "firefox",
    "opera", "safari", "vivaldi", "whale",
)

# Whole-channel browsing. "newest" is the native flat-playlist order.
SORT_ORDERS = ("newest", "oldest", "longest", "shortest", "most_viewed", "least_viewed")
SCRAPE_PAGE_SIZE = 50
SCRAPE_ALL_CAP = 2000       # entries pulled when filtering/sorting a whole channel
SCRAPE_TIMEOUT = 180        # seconds

# Disk-space thresholds for the download destination.
DISK_HARD_MIN = 500 * 1024 * 1024        # refuse to start a download below this
DISK_SOFT_WARN = 2 * 1024 * 1024 * 1024  # surface a UI warning below this

# Podcasts: the free Apple/iTunes directory for search; RSS feeds for episodes.
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
PODCAST_PAGE_SIZE = 50
PODCAST_MAX_PAGE_SIZE = 5000
HTTP_USER_AGENT = "Mozilla/5.0 (compatible; Chive; +local archival)"
