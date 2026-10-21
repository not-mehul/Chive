"""Chive bridge server.

Chive (ar·chive) — a local Flask server that drives yt-dlp for channel
scraping and SponsorBlock-aware downloads: snip the fluff, keep the good
stuff. The single-page UI in `static/index.html` talks to this server over
JSON + Server-Sent Events.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import importlib.util
from collections import deque
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlencode

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

__version__ = "1.0.0"

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


def _env(name: str) -> str | None:
    """Read a CHIVE_<name> setting, falling back to the legacy YTARCHIVE_<name>."""
    return os.environ.get(f"CHIVE_{name}") or os.environ.get(f"YTARCHIVE_{name}")


def _default_state_dir() -> Path:
    """~/.chive by default; keep using an existing ~/.ytarchive from an earlier
    install so upgrades don't orphan a user's queue and settings."""
    override = _env("STATE_DIR")
    if override:
        return Path(override)
    chive = Path.home() / ".chive"
    legacy = Path.home() / ".ytarchive"
    return legacy if (legacy.exists() and not chive.exists()) else chive


DEFAULT_DOWNLOAD_DIR = ROOT / "downloads"
DEFAULT_DOWNLOAD_DIR.mkdir(exist_ok=True)

STATE_DIR = _default_state_dir()
STATE_FILE = STATE_DIR / "state.json"
STATE_VERSION = 1
HISTORY_MAX = 10

SCRAPE_PAGE_SIZE = 50


def _env_truthy(name: str) -> bool:
    return (_env(name) or "").strip().lower() in {"1", "true", "yes", "on"}


# When CHIVE_DEBUG is set, every download's raw yt-dlp output is written —
# with per-line wall-clock offsets — to STATE_DIR/logs so streaming cadence and
# the exact line format can be inspected and shared.
DEBUG = _env_truthy("DEBUG")
DOWNLOAD_LOG_DIR = STATE_DIR / "logs"
# Per-item download logs (what the queue's log button shows). Written as lines
# arrive so they survive a server restart; deleted when the item is removed.
ITEM_LOG_DIR = STATE_DIR / "item-logs"

IS_WINDOWS = os.name == "nt"

# Categories yt-dlp can *remove* via --sponsorblock-remove. `poi_highlight` is a
# single point-of-interest marker rather than a removable span, so it is
# deliberately excluded — passing it to --sponsorblock-remove is a no-op.
SPONSORBLOCK_CATEGORIES = {
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "filler",
    "music_offtopic",
}

QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "4k": {
        "label": "4K",
        "format": "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/bv*[height<=2160]+ba/b[height<=2160]",
        "merge_output_format": "mp4",
        "audio_only": False,
    },
    "1080p": {
        "label": "1080p",
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]",
        "merge_output_format": "mp4",
        "audio_only": False,
    },
    "720p": {
        "label": "720p",
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]",
        "merge_output_format": "mp4",
        "audio_only": False,
    },
    "m4a": {
        "label": "Audio · M4A",
        "format": "ba[ext=m4a]/ba",
        "audio_only": True,
        "audio_format": "m4a",
    },
    "mp3": {
        "label": "Audio · MP3",
        "format": "ba/b",
        "audio_only": True,
        "audio_format": "mp3",
    },
}

MAX_CONCURRENCY = 4

# SponsorBlock can either cut segments out (--sponsorblock-remove, the default
# and primary behaviour) or leave the file intact and label the segments as
# chapters (--sponsorblock-mark) for skip-aware players.
SPONSORBLOCK_MODES = ("remove", "mark")

# Sort orders offered for whole-channel browsing. "newest" is the native flat-
# playlist order for a channel's uploads, so it needs no upload_date.
SORT_ORDERS = (
    "newest", "oldest", "longest", "shortest", "most_viewed", "least_viewed",
)

# Cap on how many entries we pull when filtering/sorting across a whole channel.
SCRAPE_ALL_CAP = 2000

# Disk-space thresholds for the download destination.
DISK_HARD_MIN = 500 * 1024 * 1024      # refuse to start a download below this
DISK_SOFT_WARN = 2 * 1024 * 1024 * 1024  # surface a UI warning below this

# Per-item log capacity (lines) kept in memory and returned to the viewer.
LOG_MAX_LINES = 2000

# Matches the 11-char video id our output template embeds as "[<id>]" — used to
# detect already-downloaded videos by scanning the destination tree.
_DL_ID_RE = re.compile(r"\[([0-9A-Za-z_-]{11})\]")

# --- Podcasts -------------------------------------------------------------
# Search via the free Apple/iTunes podcast directory; episodes come from each
# show's RSS feed (parsed with the stdlib XML parser). Episodes are plain audio
# enclosures downloaded by yt-dlp's generic extractor — same queue machinery.
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
PODCAST_PAGE_SIZE = 50
_HTTP_UA = "Mozilla/5.0 (compatible; Chive/1.0; +local archival)"

# yt-dlp supports cookies via --cookies-from-browser. Whitelist the values it
# accepts so the UI can offer a closed dropdown rather than free text.
COOKIE_BROWSERS = (
    "brave", "chrome", "chromium", "edge", "firefox",
    "opera", "safari", "vivaldi", "whale",
)


# ---------------------------------------------------------------------------
# yt-dlp invocation
# ---------------------------------------------------------------------------
#
# Downloads go through ytdlp_runner.py (see _ytdlp_runner_cmd) when the yt_dlp
# module is importable. _ytdlp_base is the plain invocation used for scraping,
# search, --version, and the binary download fallback.

def _ytdlp_base() -> list[str] | None:
    """Return the command prefix used to invoke yt-dlp, or None if unavailable."""
    try:
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
    except (ImportError, ValueError):
        pass
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    return None


def _ytdlp_available() -> bool:
    return _ytdlp_base() is not None


def _ytdlp_is_module() -> bool:
    base = _ytdlp_base()
    return bool(base) and base[0] == sys.executable


# A small wrapper that drives yt-dlp via its Python API with flushing progress
# hooks — the only reliable way to stream progress on Windows, where yt-dlp's
# own stdout text output is buffered and never reaches our pipe until the end.
_RUNNER_PATH = ROOT / "ytdlp_runner.py"


def _ytdlp_runner_cmd() -> list[str] | None:
    """Command to run yt-dlp via the flushing API wrapper, or None if the
    yt_dlp module isn't importable (then we fall back to the binary)."""
    if _RUNNER_PATH.exists() and importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, str(_RUNNER_PATH)]
    return None


def _ytdlp_version() -> str | None:
    base = _ytdlp_base()
    if not base:
        return None
    try:
        out = subprocess.run(base + ["--version"], capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# ---------------------------------------------------------------------------
# Queue data model
# ---------------------------------------------------------------------------


@dataclass
class QueueItem:
    id: str
    video_id: str
    url: str
    title: str
    thumbnail: str | None
    duration: int | None
    quality: str
    sponsorblock: list[str]
    sponsorblock_mode: str = "remove"  # remove | mark
    source: str = "youtube"            # youtube | podcast
    collection: str | None = None      # podcast/show name → destination subfolder
    status: str = "pending"  # pending | downloading | paused | completed | failed | cancelled
    progress: float = 0.0
    speed: str | None = None
    eta: str | None = None
    message: str | None = None
    output_file: str | None = None
    # Numeric byte/rate tracking for aggregate queue stats. Parsed from the
    # yt-dlp progress line; None until the first measurable update.
    total_bytes: float | None = None
    downloaded_bytes: float | None = None
    speed_bps: float | None = None
    added_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


# ---------------------------------------------------------------------------
# Manager: thread-safe queue + worker pool + event broadcaster
# ---------------------------------------------------------------------------


class QueueManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, QueueItem] = {}
        self._order: list[str] = []
        self._wake = threading.Event()
        self._listeners: list[queue.Queue[str]] = []
        self._listeners_lock = threading.Lock()
        self._running = False
        self._active_procs: dict[str, subprocess.Popen] = {}
        self._dispatched: set[str] = set()
        self._pausing: set[str] = set()
        self._download_dir: Path = DEFAULT_DOWNLOAD_DIR
        self._concurrency: int = 1
        self._embed_metadata: bool = True
        self._embed_thumbnail: bool = True
        self._embed_chapters: bool = True
        self._use_archive: bool = False
        self._subtitles: bool = False
        self._subtitle_langs: str = "en"
        self._subtitle_auto: bool = False
        self._subtitle_embed: bool = True
        self._cookies_browser: str = ""
        self._cookies_file: str = ""
        self._history: list[str] = []
        self._last_quality: str = "1080p"
        self._last_sponsorblock: list[str] = ["sponsor", "selfpromo"]
        self._last_sb_mode: str = "remove"
        # Per-item yt-dlp output for the log viewer: an in-memory tail, mirrored
        # to ITEM_LOG_DIR/<id>.log so it survives a restart.
        self._logs: dict[str, deque[str]] = {}
        self._log_files: dict[str, Any] = {}
        # Cached scan of the destination for already-downloaded video ids.
        self._dl_scan: dict[str, Any] | None = None
        # Serialises snapshot+write so an older snapshot can never land after
        # a newer one when two workers finish at the same moment.
        self._persist_lock = threading.Lock()
        self._shutting_down = False
        self._load_persisted()
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher.start()

    # ----- persistence -------------------------------------------------

    def _load_persisted(self) -> None:
        data = _load_state()
        if not data:
            return
        dl = data.get("download_dir")
        if isinstance(dl, str):
            try:
                p = Path(dl).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                self._download_dir = p.resolve()
            except OSError:
                pass
        try:
            self._concurrency = max(1, min(MAX_CONCURRENCY, int(data.get("concurrency", 1))))
        except (TypeError, ValueError):
            self._concurrency = 1
        self._embed_metadata = bool(data.get("embed_metadata", True))
        self._embed_thumbnail = bool(data.get("embed_thumbnail", True))
        self._embed_chapters = bool(data.get("embed_chapters", True))
        self._use_archive = bool(data.get("use_archive", False))
        self._subtitles = bool(data.get("subtitles", False))
        sl = data.get("subtitle_langs")
        if isinstance(sl, str) and sl.strip():
            self._subtitle_langs = sl.strip()
        self._subtitle_auto = bool(data.get("subtitle_auto", False))
        self._subtitle_embed = bool(data.get("subtitle_embed", True))
        cb = data.get("cookies_browser")
        if isinstance(cb, str) and cb in COOKIE_BROWSERS:
            self._cookies_browser = cb
        cf = data.get("cookies_file")
        if isinstance(cf, str):
            self._cookies_file = cf
        hist = data.get("history") or []
        if isinstance(hist, list):
            self._history = [h for h in hist if isinstance(h, str)][:HISTORY_MAX]
        lq = data.get("last_quality")
        if isinstance(lq, str) and lq in QUALITY_PRESETS:
            self._last_quality = lq
        lsb = data.get("last_sponsorblock")
        if isinstance(lsb, list):
            self._last_sponsorblock = [c for c in lsb if c in SPONSORBLOCK_CATEGORIES]
        lsm = data.get("last_sb_mode")
        if isinstance(lsm, str) and lsm in SPONSORBLOCK_MODES:
            self._last_sb_mode = lsm
        items = data.get("queue") or []
        if isinstance(items, list):
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                try:
                    item = QueueItem(
                        id=str(raw.get("id") or uuid.uuid4()),
                        video_id=str(raw["video_id"]),
                        url=str(raw["url"]),
                        title=str(raw.get("title") or raw["video_id"]),
                        thumbnail=raw.get("thumbnail"),
                        duration=raw.get("duration"),
                        quality=str(raw.get("quality") or self._last_quality),
                        sponsorblock=[c for c in (raw.get("sponsorblock") or []) if c in SPONSORBLOCK_CATEGORIES],
                        sponsorblock_mode=(raw.get("sponsorblock_mode") if raw.get("sponsorblock_mode") in SPONSORBLOCK_MODES else "remove"),
                        source=("podcast" if raw.get("source") == "podcast" else "youtube"),
                        collection=raw.get("collection"),
                        status=str(raw.get("status") or "pending"),
                        progress=float(raw.get("progress") or 0.0),
                        speed=raw.get("speed"),
                        eta=raw.get("eta"),
                        message=raw.get("message"),
                        output_file=raw.get("output_file"),
                        total_bytes=raw.get("total_bytes"),
                        downloaded_bytes=raw.get("downloaded_bytes"),
                        speed_bps=raw.get("speed_bps"),
                        added_at=float(raw.get("added_at") or time.time()),
                        started_at=raw.get("started_at"),
                        finished_at=raw.get("finished_at"),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                # interrupted downloads come back as pending so they can resume
                if item.status == "downloading":
                    item.status = "pending"
                    item.progress = 0.0
                    item.speed = None
                    item.eta = None
                    item.speed_bps = None
                    item.message = "Restored — will resume"
                self._items[item.id] = item
                self._order.append(item.id)

    def _persist_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": STATE_VERSION,
                "download_dir": str(self._download_dir),
                "concurrency": self._concurrency,
                "embed_metadata": self._embed_metadata,
                "embed_thumbnail": self._embed_thumbnail,
                "embed_chapters": self._embed_chapters,
                "use_archive": self._use_archive,
                "subtitles": self._subtitles,
                "subtitle_langs": self._subtitle_langs,
                "subtitle_auto": self._subtitle_auto,
                "subtitle_embed": self._subtitle_embed,
                "cookies_browser": self._cookies_browser,
                "cookies_file": self._cookies_file,
                "history": list(self._history),
                "last_quality": self._last_quality,
                "last_sponsorblock": list(self._last_sponsorblock),
                "last_sb_mode": self._last_sb_mode,
                "queue": [asdict(self._items[i]) for i in self._order],
            }

    def _persist(self) -> None:
        with self._persist_lock:
            try:
                _atomic_write_json(STATE_FILE, self._persist_snapshot())
            except OSError as exc:
                print(f"[chive] could not persist state: {exc}", file=sys.stderr)

    # ----- settings ----------------------------------------------------

    def set_download_dir(self, path: str) -> Path:
        p = Path(path).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._download_dir = p
        self._broadcast({"type": "settings", "download_dir": str(p)})
        self._persist()
        return p

    def set_concurrency(self, n: int) -> int:
        n = max(1, min(MAX_CONCURRENCY, int(n)))
        with self._lock:
            self._concurrency = n
        self._broadcast({"type": "settings", "concurrency": n})
        self._persist()
        self._wake.set()
        return n

    def set_embed(self, metadata: bool, thumbnail: bool, chapters: bool) -> None:
        with self._lock:
            self._embed_metadata = bool(metadata)
            self._embed_thumbnail = bool(thumbnail)
            self._embed_chapters = bool(chapters)
        self._broadcast({
            "type": "settings",
            "embed_metadata": metadata,
            "embed_thumbnail": thumbnail,
            "embed_chapters": chapters,
        })
        self._persist()

    def set_cookies(self, browser: str | None, file: str | None) -> None:
        with self._lock:
            if browser is not None:
                b = (browser or "").strip().lower()
                self._cookies_browser = b if b in COOKIE_BROWSERS else ""
            if file is not None:
                self._cookies_file = (file or "").strip()
        self._broadcast({
            "type": "settings",
            "cookies_browser": self._cookies_browser,
            "cookies_file": self._cookies_file,
        })
        self._persist()

    def set_use_archive(self, enabled: bool) -> None:
        with self._lock:
            self._use_archive = bool(enabled)
        self._broadcast({"type": "settings", "use_archive": bool(enabled)})
        self._persist()

    def set_subtitles(self, enabled: bool, langs: str | None, auto: bool, embed: bool) -> None:
        with self._lock:
            self._subtitles = bool(enabled)
            if langs is not None and langs.strip():
                self._subtitle_langs = langs.strip()
            self._subtitle_auto = bool(auto)
            self._subtitle_embed = bool(embed)
            snap = {
                "subtitles": self._subtitles,
                "subtitle_langs": self._subtitle_langs,
                "subtitle_auto": self._subtitle_auto,
                "subtitle_embed": self._subtitle_embed,
            }
        self._broadcast({"type": "settings", **snap})
        self._persist()

    def push_history(self, url: str) -> None:
        url = (url or "").strip()
        if not url:
            return
        with self._lock:
            self._history = [url] + [h for h in self._history if h != url]
            self._history = self._history[:HISTORY_MAX]
        self._broadcast({"type": "settings", "history": list(self._history)})
        self._persist()

    def remember_choices(
        self,
        quality: str | None,
        sponsorblock: list[str] | None,
        sb_mode: str | None = None,
    ) -> None:
        changed = False
        with self._lock:
            if quality and quality in QUALITY_PRESETS and quality != self._last_quality:
                self._last_quality = quality
                changed = True
            if sponsorblock is not None:
                clean = sorted([c for c in sponsorblock if c in SPONSORBLOCK_CATEGORIES])
                if clean != sorted(self._last_sponsorblock):
                    self._last_sponsorblock = clean
                    changed = True
            if sb_mode in SPONSORBLOCK_MODES and sb_mode != self._last_sb_mode:
                self._last_sb_mode = sb_mode
                changed = True
        if changed:
            self._persist()

    @property
    def download_dir(self) -> Path:
        with self._lock:
            return self._download_dir

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return {
                "download_dir": str(self._download_dir),
                "concurrency": self._concurrency,
                "max_concurrency": MAX_CONCURRENCY,
                "embed_metadata": self._embed_metadata,
                "embed_thumbnail": self._embed_thumbnail,
                "embed_chapters": self._embed_chapters,
                "use_archive": self._use_archive,
                "subtitles": self._subtitles,
                "subtitle_langs": self._subtitle_langs,
                "subtitle_auto": self._subtitle_auto,
                "subtitle_embed": self._subtitle_embed,
                "cookies_browser": self._cookies_browser,
                "cookies_file": self._cookies_file,
                "cookie_browsers": list(COOKIE_BROWSERS),
                "history": list(self._history),
                "last_quality": self._last_quality,
                "last_sponsorblock": list(self._last_sponsorblock),
                "last_sb_mode": self._last_sb_mode,
                "sb_modes": list(SPONSORBLOCK_MODES),
            }

    # ----- queue mutation ---------------------------------------------

    def add(
        self,
        videos: list[dict[str, Any]],
        quality: str,
        sponsorblock: list[str],
        sb_mode: str = "remove",
        source: str = "youtube",
    ) -> list[QueueItem]:
        if quality not in QUALITY_PRESETS:
            raise ValueError(f"unknown quality preset: {quality}")
        source = "podcast" if source == "podcast" else "youtube"
        clean_sb = [c for c in sponsorblock if c in SPONSORBLOCK_CATEGORIES]
        sb_mode = sb_mode if sb_mode in SPONSORBLOCK_MODES else "remove"
        added: list[QueueItem] = []
        with self._lock:
            # Dedup against any non-completed item already in the queue. A
            # previously failed/cancelled video should be re-run via Retry
            # rather than spawning a duplicate row; completed items may be
            # re-queued for a fresh download.
            existing_video_ids = {
                self._items[i].video_id
                for i in self._order
                if self._items[i].status != "completed"
            }
            for v in videos:
                vid = v.get("video_id") or v.get("id")
                if not vid or vid in existing_video_ids:
                    continue
                item = QueueItem(
                    id=str(uuid.uuid4()),
                    video_id=vid,
                    url=v.get("url") or (f"https://www.youtube.com/watch?v={vid}" if source == "youtube" else vid),
                    title=v.get("title") or vid,
                    thumbnail=v.get("thumbnail"),
                    duration=v.get("duration"),
                    quality=quality,
                    sponsorblock=([] if source == "podcast" else clean_sb),
                    sponsorblock_mode=sb_mode,
                    source=source,
                    collection=(v.get("collection") if source == "podcast" else None),
                )
                self._items[item.id] = item
                self._order.append(item.id)
                existing_video_ids.add(vid)
                added.append(item)
        if added:
            # One event for the whole batch: queueing a 500-episode feed used
            # to send 500 events, each triggering a full client re-render (and
            # overflowing a listener's 512-slot buffer, dropping items).
            self._broadcast({"type": "queued", "items": [asdict(i) for i in added]})
            if source == "youtube":
                self.remember_choices(quality, clean_sb, sb_mode)
            self._persist()
            self._wake.set()
        return added

    def remove(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            if item.status == "downloading":
                return False
            if item_id in self._order:
                self._order.remove(item_id)
            self._items.pop(item_id, None)
            self._logs.pop(item_id, None)
        self._delete_log_file(item_id)
        self._broadcast({"type": "removed", "id": item_id})
        self._persist()
        return True

    def retry(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            if item.status not in {"failed", "cancelled"}:
                return False
            item.status = "pending"
            item.progress = 0.0
            item.speed = None
            item.eta = None
            item.speed_bps = None
            item.message = None
            item.started_at = None
            item.finished_at = None
            # The previous attempt's log is kept (on disk and in memory); the
            # next run appends below a separator, so the failure stays visible.
            snap = asdict(item)
        self._broadcast({"type": "update", "item": snap})
        self._persist()
        self._wake.set()
        return True

    def clear_finished(self) -> int:
        dropped: list[str] = []
        with self._lock:
            keep: list[str] = []
            for iid in self._order:
                it = self._items[iid]
                if it.status in {"completed", "failed", "cancelled"}:
                    self._items.pop(iid, None)
                    self._logs.pop(iid, None)
                    dropped.append(iid)
                else:
                    keep.append(iid)
            self._order = keep
        for iid in dropped:
            self._delete_log_file(iid)
        removed = len(dropped)
        if removed:
            self._broadcast({"type": "cleared"})
            self._persist()
        return removed

    def cancel_current(self) -> int:
        with self._lock:
            procs = list(self._active_procs.values())
        cancelled = 0
        for proc in procs:
            if _terminate_proc(proc):
                cancelled += 1
        return cancelled

    def cancel_item(self, item_id: str) -> bool:
        with self._lock:
            proc = self._active_procs.get(item_id)
        if proc is None:
            return False
        return _terminate_proc(proc)

    def pause_item(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            proc = self._active_procs.get(item_id)
            if proc is None:
                # Not currently downloading — pause it in place if pending.
                if item.status == "pending":
                    item.status = "paused"
                    item.message = "Paused"
                    snap = asdict(item)
                else:
                    return False
            else:
                self._pausing.add(item_id)
                snap = None
        if snap is not None:
            self._broadcast({"type": "update", "item": snap})
            self._persist()
            return True
        return _terminate_proc(proc)

    def resume_item(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item or item.status != "paused":
                return False
            item.status = "pending"
            item.message = "Resuming"
            # Keep the last known percent so the bar holds its place until
            # yt-dlp emits a real progress line off the .part file, instead of
            # snapping back to 0%. The stale speed/eta are cleared.
            item.speed = None
            item.eta = None
            item.finished_at = None
            snap = asdict(item)
        self._broadcast({"type": "update", "item": snap})
        self._persist()
        self._wake.set()
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(self._items[i]) for i in self._order]

    # ----- already-downloaded detection -------------------------------

    def downloaded_ids(self) -> set[str]:
        """Video ids already archived locally — from completed queue items, the
        yt-dlp download archive, and a cached scan of the destination tree for
        `[<id>]` filenames (our output template embeds the id)."""
        ids: set[str] = set()
        with self._lock:
            for it in self._items.values():
                if it.status == "completed" and it.video_id:
                    ids.add(it.video_id)
            dl_dir = self._download_dir
        archive = dl_dir / "archive.txt"
        try:
            if archive.is_file():
                for line in archive.read_text(encoding="utf-8", errors="ignore").splitlines():
                    tok = line.split()
                    if tok:
                        ids.add(tok[-1])
        except OSError:
            pass
        ids |= self._scan_disk_ids(dl_dir)
        return ids

    def podcast_downloaded(self, collection: str, episodes: list[dict[str, Any]]) -> set[str]:
        """Video ids (episode guids) of `episodes` already on disk under
        <dir>/<show>/. Files are named "<title> [<tag>].<ext>" where the tag is
        derived from the guid, so same-titled episodes stay distinct. Files from
        before the tag existed ("<title>.<ext>") still match by title."""
        folder = self.download_dir / _safe_segment(collection or "Podcasts")
        tags: set[str] = set()
        stems: set[str] = set()
        try:
            if folder.is_dir():
                for p in folder.iterdir():
                    if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp"):
                        stems.add(p.stem)
                        m = _POD_TAG_RE.search(p.stem)
                        if m:
                            tags.add(m.group(1))
        except OSError:
            pass
        have: set[str] = set()
        for ep in episodes:
            vid = ep.get("video_id") or ""
            if _pod_tag(vid) in tags or _safe_segment(ep.get("title") or "") in stems:
                have.add(vid)
        return have

    def _scan_disk_ids(self, dl_dir: Path) -> set[str]:
        now = time.time()
        with self._lock:
            c = self._dl_scan
            if c and c["dir"] == str(dl_dir) and (now - c["ts"]) < 30:
                return c["ids"]
        ids: set[str] = set()
        try:
            for p in dl_dir.rglob("*"):
                # Skip in-progress artifacts so a half-finished download isn't
                # mistaken for an archived one.
                if p.suffix.lower() in (".part", ".ytdl", ".temp"):
                    continue
                if p.is_file():
                    m = _DL_ID_RE.search(p.name)
                    if m:
                        ids.add(m.group(1))
        except OSError:
            pass
        with self._lock:
            self._dl_scan = {"dir": str(dl_dir), "ts": now, "ids": ids}
        return ids

    # ----- per-item logs ----------------------------------------------

    @staticmethod
    def _log_path(item_id: str) -> Path | None:
        # Ids are uuids we mint, but restored state is user-editable JSON —
        # never let an id become a path traversal.
        if not re.fullmatch(r"[\w-]{1,80}", item_id or ""):
            return None
        return ITEM_LOG_DIR / f"{item_id}.log"

    def _open_item_log(self, item_id: str) -> None:
        path = self._log_path(item_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", encoding="utf-8")
        except OSError as exc:
            print(f"[chive] could not open item log: {exc}", file=sys.stderr)
            return
        with self._lock:
            self._log_files[item_id] = fh

    def _close_item_log(self, item_id: str) -> None:
        with self._lock:
            fh = self._log_files.pop(item_id, None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _delete_log_file(self, item_id: str) -> None:
        self._close_item_log(item_id)
        path = self._log_path(item_id)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def _log_line(self, item_id: str, line: str) -> None:
        with self._lock:
            buf = self._logs.get(item_id)
            if buf is None:
                buf = deque(maxlen=LOG_MAX_LINES)
                self._logs[item_id] = buf
            buf.append(line)
            fh = self._log_files.get(item_id)
            if fh is not None:
                try:
                    fh.write(line + "\n")
                    fh.flush()
                except (OSError, ValueError):
                    pass

    def get_log(self, item_id: str) -> list[str] | None:
        """The item's log lines — from memory this session, else from disk
        (logs outlive a restart). None when nothing was ever logged."""
        with self._lock:
            buf = self._logs.get(item_id)
            if buf is not None:
                return list(buf)
        path = self._log_path(item_id)
        if path is None or not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return [ln.rstrip("\n") for ln in deque(fh, maxlen=LOG_MAX_LINES)]
        except OSError:
            return None

    def _open_debug_log(self, item: QueueItem, cmd: list[str]):
        """When CHIVE_DEBUG is set, open a per-download log file that records
        the command and every output line with a wall-clock offset, so streaming
        cadence (vs a single end-of-run flush) is visible at a glance."""
        if not DEBUG:
            return None
        try:
            DOWNLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_vid = re.sub(r"[^\w.-]", "_", item.video_id)[:40]
            path = DOWNLOAD_LOG_DIR / f"{stamp}-{safe_vid}.log"
            fh = open(path, "w", encoding="utf-8")
            fh.write(f"# title: {item.title}\n# quality: {item.quality}\n")
            fh.write("# cmd: " + " ".join(cmd) + "\n")
            fh.write("# Each line is prefixed with seconds since the process started.\n\n")
            fh.flush()
            print(f"[chive] debug log → {path}", file=sys.stderr)
            return fh
        except OSError as exc:
            print(f"[chive] could not open debug log: {exc}", file=sys.stderr)
            return None

    # ----- aggregate stats --------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Roll up the queue into headline numbers for the UI."""
        with self._lock:
            items = [self._items[i] for i in self._order]
        active = [it for it in items if it.status == "downloading"]
        pending = [it for it in items if it.status in {"pending", "paused"}]
        completed = [it for it in items if it.status == "completed"]
        failed = [it for it in items if it.status in {"failed", "cancelled"}]
        speed_bps = sum(it.speed_bps for it in active if it.speed_bps)
        # Bytes pulled this session: finished sizes plus in-flight progress.
        downloaded = 0.0
        for it in completed:
            if it.total_bytes:
                downloaded += it.total_bytes
        for it in active:
            if it.downloaded_bytes:
                downloaded += it.downloaded_bytes
        # Remaining bytes we can actually estimate (active items with a size).
        remaining_bytes = 0.0
        measurable = False
        for it in active:
            if it.total_bytes and it.downloaded_bytes is not None:
                remaining_bytes += max(0.0, it.total_bytes - it.downloaded_bytes)
                measurable = True
        eta_seconds: float | None = None
        if measurable and speed_bps > 0:
            eta_seconds = remaining_bytes / speed_bps
        return {
            "active": len(active),
            "pending": len(pending),
            "completed": len(completed),
            "failed": len(failed),
            "remaining": len(active) + len(pending),
            "speed_bps": speed_bps,
            "downloaded_bytes": downloaded,
            "eta_seconds": eta_seconds,
        }

    def start(self) -> None:
        with self._lock:
            self._running = True
        self._wake.set()
        self._broadcast({"type": "state", "running": True})

    def pause(self) -> None:
        with self._lock:
            self._running = False
        self._broadcast({"type": "state", "running": False})

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # ----- SSE listeners -----------------------------------------------

    def listen(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=512)
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def drop(self, q: queue.Queue[str]) -> None:
        with self._listeners_lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, default=str)
        with self._listeners_lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(data)
            except queue.Full:
                # The listener is draining slower than we produce (a backgrounded
                # tab, a slow machine). Evict the OLDEST queued event and retry so
                # the newest event survives — crucially the terminal status
                # updates (completed/failed/cancelled), which would otherwise be
                # lost and leave a row stuck mid-download. Progress events are
                # throttled and newest-wins, so dropping an older one is harmless.
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass

    # ----- dispatcher + workers ----------------------------------------

    def _dispatch_loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                spawned = False
                with self._lock:
                    if not self._running:
                        break
                    if len(self._active_procs) + len(self._dispatched) >= self._concurrency:
                        break
                    next_item: QueueItem | None = None
                    for iid in self._order:
                        it = self._items[iid]
                        if (
                            it.status == "pending"
                            and iid not in self._active_procs
                            and iid not in self._dispatched
                        ):
                            next_item = it
                            break
                    if next_item is None:
                        break
                    self._dispatched.add(next_item.id)
                    spawned = True
                if spawned and next_item is not None:
                    t = threading.Thread(target=self._process, args=(next_item,), daemon=True)
                    t.start()
                if not spawned:
                    break

    def _process(self, item: QueueItem) -> None:
        self._begin_item_log(item)
        # Refuse to start if the destination is critically low on space — a
        # failed write halfway through is worse than not starting.
        free = _disk_free(self.download_dir)
        if free is not None and free < DISK_HARD_MIN:
            with self._lock:
                self._dispatched.discard(item.id)
            self._broadcast({"type": "disk", "free_bytes": free, "low": True})
            self._end_item(item, "failed",
                           f"Insufficient disk space ({_human_bytes(free)} free)")
            self._wake.set()
            return

        with self._lock:
            item.status = "downloading"
            item.started_at = time.time()
        self._broadcast({"type": "update", "item": asdict(item)})

        cmd = self._build_command(item)
        self._log_line(item.id, "$ " + " ".join(cmd))
        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        # Own process group, so cancelling one download can signal its whole
        # tree (yt-dlp + ffmpeg). Because the group is detached from the
        # console, shutdown() is what stops them when the server exits.
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as exc:
            with self._lock:
                self._dispatched.discard(item.id)
            msg = "yt-dlp not found on PATH" if isinstance(exc, FileNotFoundError) else str(exc)
            self._end_item(item, "failed", msg)
            self._wake.set()
            return
        with self._lock:
            if self._shutting_down:
                # Raced with shutdown(): don't leave an orphan behind.
                self._dispatched.discard(item.id)
                _force_kill(proc)
                return
            self._active_procs[item.id] = proc
            self._dispatched.discard(item.id)

        last_emit = 0.0
        last_progress = -1.0
        last_message: str | None = None
        logged_step = -1
        archived = False
        already_on_disk = False
        debug_fh = self._open_debug_log(item, cmd)
        t0 = time.time()
        assert proc.stdout is not None
        try:
            # readline() rather than `for line in stdout` — iterator read-ahead
            # can buffer lines on some platforms, which would make progress
            # surface as a single 0%→100% jump at the end.
            for raw in iter(proc.stdout.readline, ""):
                line = raw.rstrip("\n").rstrip("\r")
                if debug_fh is not None:
                    try:
                        debug_fh.write(f"[+{time.time() - t0:8.3f}s] {line}\n")
                        debug_fh.flush()
                    except OSError:
                        pass
                if "has already been recorded in the archive" in line:
                    archived = True
                elif "has already been downloaded" in line:
                    already_on_disk = True
                self._parse_progress(item, line)
                if "[YTPROG]" in line:
                    # Raw progress lines arrive many times a second; the log
                    # gets a readable summary every 10% instead.
                    step = int(item.progress // 10)
                    if step < logged_step:
                        logged_step = -1          # a new stream started
                    if step > logged_step:
                        logged_step = step
                        self._log_line(item.id, self._progress_summary(item))
                elif line.startswith("[YTPP]"):
                    self._log_line(item.id, "[postprocess] " + line[6:].strip())
                else:
                    self._log_line(item.id, line)
                now = time.time()
                changed = item.progress != last_progress or item.message != last_message
                if changed and now - last_emit > 0.15:
                    self._broadcast({"type": "progress", "item": asdict(item)})
                    last_emit = now
                    last_progress = item.progress
                    last_message = item.message
        except Exception as exc:  # keep the worker alive, but leave a trace
            self._log_line(item.id, f"[chive] output reader error: {exc!r}")
        finally:
            if debug_fh is not None:
                try:
                    debug_fh.close()
                except OSError:
                    pass
            try:
                proc.stdout.close()    # otherwise every download leaks its pipe
            except OSError:
                pass
        rc = proc.wait()
        with self._lock:
            self._active_procs.pop(item.id, None)
            paused = item.id in self._pausing
            self._pausing.discard(item.id)
            shutting_down = self._shutting_down
        if shutting_down:
            # Server is exiting: leave the item as-is so it is restored as
            # pending and resumes from its .part file on the next launch.
            self._log_line(item.id, "[chive] server shutting down — will resume on next launch")
            self._close_item_log(item.id)
            return
        # Flush any tail update so the bar reaches its final value before
        # the completion event arrives.
        if item.progress != last_progress or item.message != last_message:
            self._broadcast({"type": "progress", "item": asdict(item)})
        if paused:
            self._end_item(item, "paused", "Paused — partial file kept for resume")
        elif rc == 0 and archived:
            self._end_item(item, "completed", "Skipped — already in archive")
        elif rc == 0 and already_on_disk:
            self._end_item(item, "completed", "Already on disk")
        elif rc == 0:
            self._end_item(item, "completed", "Saved")
        elif rc < 0 or (IS_WINDOWS and rc in (3221225786, -1073741510)):
            # CTRL_BREAK/CTRL_C terminations on Windows surface as 0xC000013A
            self._end_item(item, "cancelled", "Stopped")
        else:
            self._end_item(item, "failed", item.message or f"yt-dlp exited {rc}")
        self._wake.set()

    def _begin_item_log(self, item: QueueItem) -> None:
        """Open the item's log for a new attempt. Earlier attempts (a retry,
        a resume, a previous server session) are kept above a separator."""
        with self._lock:
            have_memory = item.id in self._logs
        if not have_memory:
            previous = self.get_log(item.id) or []
            with self._lock:
                self._logs[item.id] = deque(previous, maxlen=LOG_MAX_LINES)
        self._open_item_log(item.id)
        with self._lock:
            started_before = bool(self._logs[item.id])
        if started_before:
            self._log_line(item.id, "")
        self._log_line(item.id, f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} · "
                                f"{'resuming' if started_before else 'starting'}: {item.title}")

    def _end_item(self, item: QueueItem, status: str, message: str) -> None:
        self._log_line(item.id, f"=== {status}: {message}")
        self._close_item_log(item.id)
        self._finish(item, status, message)

    @staticmethod
    def _progress_summary(item: QueueItem) -> str:
        parts = [f"[progress] {item.progress:5.1f}%"]
        if item.total_bytes:
            parts.append(f"of {_human_bytes(item.total_bytes)}")
        if item.speed:
            parts.append(f"at {item.speed}")
        if item.eta:
            parts.append(f"eta {item.eta}")
        return " ".join(parts)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop every running download before the server exits. Downloads run
        in their own process group, so they would otherwise keep going after
        the server is gone — and, being restored as pending on the next launch,
        a second process would then write to the same .part file."""
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            self._running = False
            procs = list(self._active_procs.values())
        for proc in procs:
            _terminate_proc(proc, escalate=False)
        deadline = time.time() + timeout
        for proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                _force_kill(proc)
        self._persist()

    def _finish(self, item: QueueItem, status: str, message: str) -> None:
        with self._lock:
            item.status = status
            item.message = message
            item.finished_at = time.time()
            item.speed = None
            item.eta = None
            item.speed_bps = None
            if status == "completed":
                item.progress = 100.0
                # Prefer the real file size for archived-size accounting.
                if item.output_file:
                    try:
                        item.total_bytes = float(os.path.getsize(item.output_file))
                    except OSError:
                        pass
                if item.total_bytes:
                    item.downloaded_bytes = item.total_bytes
        self._broadcast({"type": "update", "item": asdict(item)})
        self._persist()

    # ----- yt-dlp command + parsing ------------------------------------

    # Machine-readable progress line for the BINARY fallback (no yt_dlp module):
    # raw numeric fields emitted via --progress-template. The preferred path is
    # ytdlp_runner.py, which emits the identical "[YTPROG]" line from a progress
    # hook with an explicit flush — the only thing that reliably streams on
    # Windows, where yt-dlp buffers its own stdout text output.
    _PROGRESS_TPL = (
        "download:[YTPROG] %(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
        "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
        "%(progress.status)s|%(info.ext)s"
    )

    def _cmd_prefix(self, out_template: str) -> list[str]:
        """Shared yt-dlp invocation: the flushing API wrapper when available
        (the only thing that streams progress on Windows), else the binary +
        --progress-template. Both yield the same "[YTPROG]" lines we parse."""
        runner = _ytdlp_runner_cmd()
        cmd = list(runner) if runner is not None else list(_ytdlp_base() or ["yt-dlp"])
        cmd += ["--newline", "--no-colors", "--no-playlist"]
        if runner is None:
            cmd += ["--progress-template", self._PROGRESS_TPL]
        # --print implies --quiet, which silences every status line we rely on
        # ("…already been recorded in the archive", destinations, errors in
        # context) and left the log viewer nearly empty. --no-quiet undoes that
        # while --print still reports the final path.
        cmd += ["-o", out_template, "--print", "after_move:filepath:%(filepath)s", "--no-quiet"]
        return cmd

    def _build_command(self, item: QueueItem) -> list[str]:
        if item.source == "podcast":
            return self._build_podcast_command(item)
        preset = QUALITY_PRESETS[item.quality]
        out_template = str(self._download_dir / "%(uploader)s/%(title)s [%(id)s].%(ext)s")
        cmd = self._cmd_prefix(out_template)
        with self._lock:
            audio_only = bool(preset.get("audio_only"))
            if audio_only:
                cmd += [
                    "-f", preset["format"],
                    "-x",
                    "--audio-format", preset["audio_format"],
                ]
            else:
                cmd += ["-f", preset["format"]]
                if "merge_output_format" in preset:
                    cmd += ["--merge-output-format", preset["merge_output_format"]]
            if item.sponsorblock:
                # Remove (cut the segments out) is the primary behaviour; mark
                # leaves the file intact and labels segments as chapters.
                if item.sponsorblock_mode == "mark":
                    cmd += ["--sponsorblock-mark", ",".join(item.sponsorblock)]
                else:
                    cmd += ["--sponsorblock-remove", ",".join(item.sponsorblock)]
            # Subtitles (video presets only — can't embed into a bare audio file).
            # When segments are *removed*, embedding keeps captions in sync
            # because yt-dlp's ModifyChapters postprocessor cuts the embedded
            # subtitle streams along with audio/video. Sidecar .srt files are
            # NOT re-timed, so embed is the default and the synced choice.
            if self._subtitles and not audio_only:
                langs = self._subtitle_langs.strip() or "en"
                cmd += ["--sub-langs", langs, "--write-subs"]
                if self._subtitle_auto:
                    cmd += ["--write-auto-subs"]
                if self._subtitle_embed:
                    cmd += ["--embed-subs"]
            if self._embed_metadata:
                cmd += ["--embed-metadata"]
            if self._embed_thumbnail:
                cmd += ["--embed-thumbnail"]
            # Chapters help skip-aware players, and are required for SponsorBlock
            # "mark" mode to be visible — enable them implicitly when marking.
            if (self._embed_chapters or item.sponsorblock_mode == "mark") and not audio_only:
                cmd += ["--embed-chapters"]
            if self._use_archive:
                archive_path = self._download_dir / "archive.txt"
                cmd += ["--download-archive", str(archive_path)]
            if self._cookies_browser:
                cmd += ["--cookies-from-browser", self._cookies_browser]
            elif self._cookies_file:
                cmd += ["--cookies", self._cookies_file]
        cmd.append(item.url)
        return cmd

    def _build_podcast_command(self, item: QueueItem) -> list[str]:
        # A podcast episode is a plain audio enclosure: download it as-is (no
        # format selection / SponsorBlock / merge) into <dir>/<show>/<episode>.
        # The title/show are literals we already know, so we sanitise them
        # ourselves rather than relying on the generic extractor's metadata.
        # The [tag] (from the episode guid) keeps same-titled episodes —
        # "Trailer", "Bonus" — from resolving to one file, where yt-dlp would
        # skip the second as "already downloaded" and report it as saved.
        safe_show = _safe_segment(item.collection or "Podcasts")
        safe_title = _safe_segment(item.title)
        name = f"{safe_title} [{_pod_tag(item.video_id)}].%(ext)s"
        out = str(self.download_dir / safe_show / name)
        cmd = self._cmd_prefix(out)
        with self._lock:
            if self._embed_metadata:
                cmd += ["--embed-metadata"]
            if self._cookies_browser:
                cmd += ["--cookies-from-browser", self._cookies_browser]
            elif self._cookies_file:
                cmd += ["--cookies", self._cookies_file]
        cmd.append(item.url)
        return cmd

    _FILEPATH_RE = re.compile(r"^filepath:(.+)$")
    _ERROR_RE = re.compile(r"^ERROR:\s*(.+)$", re.IGNORECASE)
    _DESTINATION_RE = re.compile(r"^\[download\]\s+Destination:\s*(.+)$")

    _AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".aac", ".wav", ".flac", ".ogg"}
    _VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi", ".ts"}
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

    def _classify_destination(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in self._AUDIO_EXTS:
            return "Downloading audio"
        if ext in self._VIDEO_EXTS:
            return "Downloading video"
        if ext in self._IMAGE_EXTS:
            return "Downloading thumbnail"
        return "Downloading"

    def _phase_for_ext(self, ext: str | None) -> str:
        if not ext or ext in ("NA", "none"):
            return "Downloading"
        e = ext.lower()
        if not e.startswith("."):
            e = "." + e
        if e in self._AUDIO_EXTS:
            return "Downloading audio"
        if e in self._VIDEO_EXTS:
            return "Downloading video"
        return "Downloading"

    @staticmethod
    def _tpl_num(v: str) -> float | None:
        v = (v or "").strip()
        if not v or v in ("NA", "N/A", "None", "none"):
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def _parse_tpl_progress(self, item: QueueItem, line: str) -> None:
        """Parse our --progress-template line: raw downloaded/total/estimate/
        speed/eta/status/ext, pipe-separated."""
        try:
            payload = line.split("[YTPROG]", 1)[1].strip()
        except IndexError:
            return
        parts = [p.strip() for p in payload.split("|")]
        if len(parts) < 7:
            return
        downloaded = self._tpl_num(parts[0])
        total = self._tpl_num(parts[1]) or self._tpl_num(parts[2])
        speed_bps = self._tpl_num(parts[3])
        eta_s = self._tpl_num(parts[4])
        status = parts[5]
        ext = parts[6]
        with self._lock:
            if total and downloaded is not None:
                item.total_bytes = total
                item.downloaded_bytes = downloaded
                item.progress = max(0.0, min(100.0, downloaded / total * 100.0))
            elif downloaded is not None:
                item.downloaded_bytes = downloaded
            item.speed_bps = speed_bps
            item.speed = (_human_bytes(speed_bps) + "/s") if speed_bps else None
            item.eta = _fmt_eta(eta_s)
            if status == "finished" and item.total_bytes:
                item.progress = 100.0
            item.message = self._phase_for_ext(ext)

    def _pp_phase(self, name: str) -> str:
        n = (name or "").lower()
        if "merger" in n:
            return "Merging streams"
        if "modifychapters" in n or "sponsorblock" in n:
            return "Cutting segments"
        if "extractaudio" in n:
            return "Extracting audio"
        if "embedsubtitle" in n or "subtitlesconvert" in n:
            return "Embedding subtitles"
        if "embedthumbnail" in n:
            return "Embedding thumbnail"
        if "metadata" in n:
            return "Writing metadata"
        if "embedchapter" in n:
            return "Writing chapters"
        return "Processing"

    def _looks_age_restricted(self, text: str) -> bool:
        t = text.lower()
        return (
            "sign in to confirm your age" in t
            or "age-restricted" in t
            or "inappropriate for some users" in t
            or "confirm you're not a bot" in t
        )

    def _parse_progress(self, item: QueueItem, line: str) -> None:
        # Strip carriage returns; yt-dlp on Windows emits CRLF.
        line = line.rstrip("\r")
        if line.startswith("[YTPP]"):
            with self._lock:
                item.message = self._pp_phase(line[6:].strip())
            return
        if "[YTPROG]" in line:
            self._parse_tpl_progress(item, line)
            return
        dest = self._DESTINATION_RE.match(line)
        if dest:
            phase = self._classify_destination(dest.group(1).strip())
            with self._lock:
                item.message = phase
                item.progress = 0.0
                item.speed = None
                item.eta = None
            return
        fp = self._FILEPATH_RE.match(line)
        if fp:
            with self._lock:
                item.output_file = fp.group(1).strip()
            return
        if "[SponsorBlock]" in line:
            with self._lock:
                item.message = "Querying SponsorBlock"
            return
        if "[Merger]" in line or "Merging" in line:
            with self._lock:
                item.message = "Merging streams"
            return
        if "[ExtractAudio]" in line:
            with self._lock:
                item.message = "Extracting audio"
            return
        if "[EmbedThumbnail]" in line:
            with self._lock:
                item.message = "Embedding thumbnail"
            return
        if "[Metadata]" in line:
            with self._lock:
                item.message = "Writing metadata"
            return
        if "Deleting original" in line or "[ModifyChapters]" in line:
            with self._lock:
                item.message = "Cutting segments"
            return
        err = self._ERROR_RE.match(line)
        if err:
            msg = err.group(1)
            if self._looks_age_restricted(msg):
                msg = "Age-restricted — add cookies under Settings → Access & throughput"
            with self._lock:
                item.message = msg[:200]
            return
        if self._looks_age_restricted(line):
            with self._lock:
                item.message = "Age-restricted — add cookies under Settings → Access & throughput"


# ---------------------------------------------------------------------------
# Byte / size / disk helpers
# ---------------------------------------------------------------------------

def _human_bytes(n: float | None) -> str:
    if not n or n <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i else f"{int(f)} {units[i]}"


def _fmt_eta(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _disk_free(path: Path) -> int | None:
    """Free bytes on the filesystem holding `path` (or its nearest parent)."""
    p = path
    for _ in range(6):
        try:
            return shutil.disk_usage(str(p)).free
        except (FileNotFoundError, NotADirectoryError):
            p = p.parent
        except OSError:
            return None
    return None


TERMINATE_GRACE = 10  # seconds to wait after SIGTERM before SIGKILL


def _force_kill(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _escalate_after_grace(proc: subprocess.Popen) -> None:
    # A yt-dlp/ffmpeg child that ignores SIGTERM would otherwise leave the
    # worker thread blocked on proc.wait() forever, permanently consuming a
    # concurrency slot. Escalate to SIGKILL once the grace period lapses.
    try:
        proc.wait(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        _force_kill(proc)


def _terminate_proc(proc: subprocess.Popen, escalate: bool = True) -> bool:
    if proc.poll() is not None:
        return False
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
    except (OSError, ValueError):
        try:
            proc.terminate()
        except Exception:
            return False
    if escalate:
        threading.Thread(target=_escalate_after_grace, args=(proc,), daemon=True).start()
    return True


manager = QueueManager()


# ---------------------------------------------------------------------------
# Reveal helper
# ---------------------------------------------------------------------------


def _reveal_path(target: Path) -> None:
    """Open the OS file manager focused on `target` (or its parent if a file)."""
    if not target.exists():
        raise FileNotFoundError(str(target))
    try:
        if IS_WINDOWS:
            if target.is_file():
                subprocess.Popen(["explorer", f"/select,{target}"])
            else:
                subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            if target.is_file():
                subprocess.Popen(["open", "-R", str(target)])
            else:
                subprocess.Popen(["open", str(target)])
        else:
            opener = "xdg-open"
            folder = target if target.is_dir() else target.parent
            subprocess.Popen([opener, str(folder)])
    except FileNotFoundError as exc:
        raise RuntimeError(f"could not open file manager: {exc}") from exc


# ---------------------------------------------------------------------------
# Scrape helpers
# ---------------------------------------------------------------------------


def _thumbnail_for(video_id: str, hinted: str | None) -> str | None:
    if hinted:
        return hinted
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def _is_short(entry_url: str | None) -> bool:
    # The `/shorts/` URL path is the only reliable signal. A duration-based
    # heuristic was tried here but hid legitimate short uploads (clips, teasers,
    # trailers) — plenty of real videos run under a minute, and the Shorts
    # length ceiling has since risen to three minutes, so duration tells us
    # nothing useful. Shorts come through flat-playlist with `/shorts/` URLs.
    return bool(entry_url and "/shorts/" in entry_url)


def _entry_to_video(entry: dict[str, Any]) -> dict[str, Any] | None:
    vid = entry.get("id")
    if not vid:
        return None
    entry_url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
    thumb = None
    thumbs = entry.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        thumb = thumbs[-1].get("url")
    elif entry.get("thumbnail"):
        thumb = entry["thumbnail"]
    return {
        "video_id": vid,
        "url": entry_url,
        "title": entry.get("title") or vid,
        "thumbnail": _thumbnail_for(vid, thumb),
        "duration": entry.get("duration"),
        "upload_date": entry.get("upload_date"),
        "view_count": entry.get("view_count"),
        "channel": entry.get("channel") or entry.get("uploader"),
    }


def _norm_date(s: str | None) -> str | None:
    """Normalise 'YYYY-MM-DD' or 'YYYYMMDD' to the compact 'YYYYMMDD' form."""
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return digits[:8] if len(digits) >= 8 else None


def _passes_filters(v: dict[str, Any], f: dict[str, Any]) -> bool:
    """Apply optional filters. A missing field on the entry passes that filter —
    flat-playlist often omits view_count / upload_date, and an archival tool
    should not silently hide videos it merely lacks metadata for."""
    q = f.get("query")
    if q and q.lower() not in (v.get("title") or "").lower():
        return False
    dur = v.get("duration")
    if dur is not None:
        if f.get("duration_min") is not None and dur < f["duration_min"]:
            return False
        if f.get("duration_max") is not None and dur > f["duration_max"]:
            return False
    views = v.get("view_count")
    if views is not None:
        if f.get("views_min") is not None and views < f["views_min"]:
            return False
        if f.get("views_max") is not None and views > f["views_max"]:
            return False
    ud = v.get("upload_date")
    if ud:
        if f.get("date_from") and ud < f["date_from"]:
            return False
        if f.get("date_to") and ud > f["date_to"]:
            return False
    return True


def _sort_videos(videos: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    # The incoming order is the channel's native newest-first flat order.
    if sort == "newest":
        return videos
    if sort == "oldest":
        return list(reversed(videos))
    if sort == "longest":
        return sorted(videos, key=lambda v: (v.get("duration") or -1), reverse=True)
    if sort == "shortest":
        return sorted(videos, key=lambda v: (v.get("duration") if v.get("duration") is not None else 1e18))
    if sort == "most_viewed":
        return sorted(videos, key=lambda v: (v.get("view_count") or -1), reverse=True)
    if sort == "least_viewed":
        return sorted(videos, key=lambda v: (v.get("view_count") if v.get("view_count") is not None else 1e18))
    return videos


def _filters_active(f: dict[str, Any], sort: str) -> bool:
    keys = ("query", "duration_min", "duration_max", "views_min", "views_max", "date_from", "date_to")
    return any(f.get(k) not in (None, "") for k in keys) or (sort and sort != "newest")


# Per-URL cache of parsed flat-playlist entries. Because yt-dlp can only walk a
# channel's continuation tokens from the top (see below), every page fetch is a
# "1:end" scrape — so the deepest fetch already contains every shallower page.
# Caching it makes back-navigation, Shorts-toggling, and re-renders instant
# instead of re-running yt-dlp each time.
_SCRAPE_CACHE: dict[str, dict[str, Any]] = {}
_SCRAPE_CACHE_LOCK = threading.Lock()
_SCRAPE_CACHE_TTL = 300  # seconds


def _fetch_entries(url: str, end: int) -> list[dict[str, Any]]:
    """Return parsed flat-playlist entries for `url`, covering items 1..end.

    Served from the per-URL cache when a fresh, deep-enough scrape already
    exists; otherwise yt-dlp is run for `1:end` and the result is cached.
    """
    now = time.time()
    with _SCRAPE_CACHE_LOCK:
        cached = _SCRAPE_CACHE.get(url)
        if cached and cached["end"] >= end and (now - cached["ts"]) < _SCRAPE_CACHE_TTL:
            return cached["entries"]
    # For channel URLs yt-dlp's --playlist-items "51:100" often returns nothing
    # because it never walks the continuation tokens past the first batch. Ask
    # for 1:end and slice client-side — yt-dlp keeps paginating until it hits
    # the upper bound, which is what we actually want.
    cmd = list(_ytdlp_base() or ["yt-dlp"]) + [
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--ignore-no-formats-error",
        "--playlist-items", f"1:{end}",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0 and not proc.stdout.strip():
        msg = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else "scrape failed"
        raise RuntimeError(msg)
    entries: list[dict[str, Any]] = []
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            entries.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    with _SCRAPE_CACHE_LOCK:
        _SCRAPE_CACHE[url] = {"entries": entries, "end": end, "ts": now}
    return entries


def scrape_channel(
    url: str,
    page: int,
    page_size: int,
    ignore_shorts: bool = True,
    filters: dict[str, Any] | None = None,
    sort: str = "newest",
) -> dict[str, Any]:
    """Return a page of videos from a channel/playlist URL.

    Without filters/sort this pages a sliding window of the channel (fast).
    When any filter or a non-default sort is active it pulls the whole channel
    (capped) and paginates the filtered+sorted result, so search/sort apply
    across the entire channel rather than just the current page.
    """
    if not _ytdlp_available():
        raise RuntimeError("yt-dlp is not installed or not on PATH")
    filters = filters or {}
    sort = sort if sort in SORT_ORDERS else "newest"

    if not _filters_active(filters, sort):
        start = (page - 1) * page_size + 1
        end = page * page_size
        entries = _fetch_entries(url, end)
        # Drop everything before this page's window so we don't re-emit page 1.
        window = entries[start - 1:end]
        # Entries present in this window before Shorts filtering. The UI uses
        # this (not the post-filter count) to decide whether a further page
        # exists — a window full of hidden Shorts still has pages behind it.
        page_entries = len(window)
        videos: list[dict[str, Any]] = []
        channel: str | None = None
        skipped_shorts = 0
        for entry in window:
            v = _entry_to_video(entry)
            if v is None:
                continue
            if not channel:
                channel = v.get("channel")
            if ignore_shorts and _is_short(v["url"]):
                skipped_shorts += 1
                continue
            videos.append(v)
        return {
            "channel": channel, "videos": videos, "page": page,
            "page_size": page_size, "count": len(videos),
            "page_entries": page_entries, "skipped_shorts": skipped_shorts,
            "ignore_shorts": ignore_shorts, "filtered": False,
        }

    # Filtered/sorted: scan the whole (capped) channel.
    entries = _fetch_entries(url, SCRAPE_ALL_CAP)
    channel = None
    skipped_shorts = 0
    matched: list[dict[str, Any]] = []
    for entry in entries:
        v = _entry_to_video(entry)
        if v is None:
            continue
        if not channel:
            channel = v.get("channel")
        if ignore_shorts and _is_short(v["url"]):
            skipped_shorts += 1
            continue
        if _passes_filters(v, filters):
            matched.append(v)
    matched = _sort_videos(matched, sort)
    total = len(matched)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return {
        "channel": channel, "videos": matched[start:start + page_size],
        "page": page, "page_size": page_size, "count": len(matched[start:start + page_size]),
        "total": total, "total_pages": total_pages, "scanned": len(entries),
        "capped": len(entries) >= SCRAPE_ALL_CAP, "skipped_shorts": skipped_shorts,
        "ignore_shorts": ignore_shorts, "filtered": True,
    }


def validate_source(text: str) -> tuple[str, str]:
    """Classify the search box input. Returns (kind, value) where kind is
    'url' (value is a normalised URL) or 'query' (value is a search string).
    Raises ValueError for input that is clearly neither usable."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Enter a channel URL, @handle, or a name to search")
    # @handle shorthand → canonical YouTube channel URL.
    if text.startswith("@") and " " not in text:
        return "url", f"https://www.youtube.com/{text}"
    parsed = urlparse(text if "://" in text else "https://" + text)
    looks_like_host = "." in (parsed.netloc or "") and " " not in text
    if "://" in text or looks_like_host:
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported URL scheme: {parsed.scheme or 'none'}")
        if not parsed.netloc:
            raise ValueError("That does not look like a valid URL")
        normalised = text if "://" in text else "https://" + text
        return "url", normalised
    # Plain text → search query.
    return "query", text


def search_channels(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Find candidate channels by plain-text name via a small video search,
    deduped by channel. yt-dlp has no dedicated channel-search, so we mine the
    channel fields off a `ytsearch` of videos."""
    if not _ytdlp_available():
        raise RuntimeError("yt-dlp is not installed or not on PATH")
    cmd = list(_ytdlp_base() or ["yt-dlp"]) + [
        "--flat-playlist", "--dump-json", "--no-warnings",
        "--ignore-no-formats-error",
        f"ytsearch{max(limit * 4, 12)}:{query}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0 and not proc.stdout.strip():
        msg = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else "search failed"
        raise RuntimeError(msg)
    seen: dict[str, dict[str, Any]] = {}
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        name = e.get("channel") or e.get("uploader")
        curl = e.get("channel_url") or e.get("uploader_url")
        cid = e.get("channel_id") or curl or name
        if not name or not curl or cid in seen:
            continue
        seen[cid] = {"name": name, "url": curl, "channel_id": e.get("channel_id")}
        if len(seen) >= limit:
            break
    return list(seen.values())


# ---------------------------------------------------------------------------
# Podcasts
# ---------------------------------------------------------------------------

_FS_BAD = re.compile(r'[<>:"/\\|?*%\x00-\x1f]')


def _safe_segment(name: str) -> str:
    """A filesystem-safe path segment for a podcast show/episode. Also strips
    `%` so it is safe to use as literal text in a yt-dlp output template."""
    s = _FS_BAD.sub("", name or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s[:120] or "untitled"


_POD_TAG_RE = re.compile(r"\[([0-9a-f]{8})\]$")


def _pod_tag(guid: str) -> str:
    """Short stable tag for a podcast episode, embedded in its filename."""
    return hashlib.sha1((guid or "").encode("utf-8")).hexdigest()[:8]


def _require_http_url(url: str) -> str:
    """Only http(s) URLs may be fetched. urllib would otherwise happily read
    file:// (local files) or ftp:// on the strength of a client-supplied URL."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("feed URL must be an http(s) address")
    return url.strip()


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_http_url(newurl)   # a redirect must not escape to file:// etc.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTP_OPENER = urllib.request.build_opener(_HttpOnlyRedirects)


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(_require_http_url(url),
                                 headers={"User-Agent": _HTTP_UA, "Accept": "*/*"})
    with _HTTP_OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def _parse_pod_duration(raw: Any) -> int | None:
    """iTunes durations come as seconds ("3600") or clock strings ("1:02:03")."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def search_podcasts(term: str, limit: int = 12) -> list[dict[str, Any]]:
    """Search the Apple/iTunes podcast directory (free, no key)."""
    q = urlencode({"term": term, "media": "podcast", "entity": "podcast", "limit": limit})
    try:
        data = json.loads(_http_get(f"{ITUNES_SEARCH_URL}?{q}", timeout=20))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"podcast search failed: {exc}")
    shows: list[dict[str, Any]] = []
    for r in data.get("results", []):
        feed = r.get("feedUrl")
        if not feed:
            continue
        shows.append({
            "name": r.get("collectionName") or r.get("trackName") or "Podcast",
            "author": r.get("artistName"),
            "feed_url": feed,
            "artwork": r.get("artworkUrl600") or r.get("artworkUrl100"),
            "episode_count": r.get("trackCount"),
            "genre": r.get("primaryGenreName"),
        })
    return shows


_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def _rss_date(raw: str | None) -> str | None:
    """RFC-822 pubDate → compact YYYYMMDD (matches the YouTube card format)."""
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y%m%d") if dt else None
    except (TypeError, ValueError, IndexError):
        return None


def fetch_episodes(feed_url: str, page: int, page_size: int = PODCAST_PAGE_SIZE,
                   query: str | None = None) -> dict[str, Any]:
    """Parse a podcast RSS feed and return one page of episodes, shaped like the
    scrape result (so the UI grid renders them the same way). Uses the stdlib
    XML parser — podcast feeds are standard RSS 2.0, no third-party dep needed."""
    import xml.etree.ElementTree as ET
    try:
        raw = _http_get(feed_url, timeout=30)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"could not fetch feed: {exc}")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"could not parse feed: {exc}")
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("that doesn't look like a podcast RSS feed")
    show = (channel.findtext("title") or "").strip() or "Podcast"
    show_image = None
    img = channel.find("image")
    if img is not None:
        show_image = (img.findtext("url") or "").strip() or None
    if not show_image:
        it_img = channel.find(f"{{{_ITUNES_NS}}}image")
        if it_img is not None:
            show_image = it_img.get("href")
    episodes: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if not url:
            continue
        ep_img = item.find(f"{{{_ITUNES_NS}}}image")
        thumb = ep_img.get("href") if ep_img is not None else show_image
        episodes.append({
            "video_id": (item.findtext("guid") or "").strip() or url,
            "url": url,
            "title": (item.findtext("title") or "Episode").strip(),
            "thumbnail": thumb,
            "duration": _parse_pod_duration(item.findtext(f"{{{_ITUNES_NS}}}duration")),
            "upload_date": _rss_date(item.findtext("pubDate")),
            "collection": show,
        })
    if query:
        ql = query.lower()
        episodes = [v for v in episodes if ql in v["title"].lower()]
    total = len(episodes)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    window = episodes[start:start + page_size]
    return {
        "podcast": show, "artwork": show_image, "feed_url": feed_url,
        "videos": window, "page": page, "page_size": page_size,
        "count": len(window), "total": total, "total_pages": total_pages,
        "source": "podcast",
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.errorhandler(Exception)
def _api_error(exc: Exception):
    """API callers always get JSON, never an HTML traceback page."""
    if isinstance(exc, HTTPException):
        if not request.path.startswith("/api/"):
            return exc
        return jsonify({"error": exc.description}), exc.code
    app.logger.exception("unhandled error on %s", request.path)
    return jsonify({"error": str(exc) or exc.__class__.__name__}), 500


@app.route("/")
def index() -> Response:
    return send_from_directory(STATIC_DIR, "index.html")


def _tool_version(name: str) -> str | None:
    if shutil.which(name) is None:
        return None
    try:
        out = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@app.route("/api/health")
def health() -> Response:
    dl_dir = manager.download_dir
    free = _disk_free(dl_dir)
    total = None
    try:
        total = shutil.disk_usage(str(dl_dir)).total
    except OSError:
        pass
    return jsonify({
        "ok": True,
        "version": __version__,
        "yt_dlp": _ytdlp_available(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "yt_dlp_version": _ytdlp_version(),
        # "module" streams progress reliably; "binary" can buffer on Windows.
        "yt_dlp_source": "module" if _ytdlp_is_module() else ("binary" if _ytdlp_available() else None),
        "ffmpeg_version": (_tool_version("ffmpeg") or "").replace("ffmpeg version ", "")[:24] or None,
        "download_dir": str(dl_dir),
        "disk_free": free,
        "disk_total": total,
        "disk_low": free is not None and free < DISK_SOFT_WARN,
    })


def _parse_int(data: dict[str, Any], key: str) -> int | None:
    val = data.get(key)
    if val in (None, ""):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _int_arg(data: dict[str, Any], key: str, default: int,
             lo: int | None = None, hi: int | None = None) -> int:
    """An integer request field, falling back to `default` when missing or
    malformed, clamped to [lo, hi]."""
    val = _parse_int(data, key)
    val = default if val is None else val
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


@app.route("/api/search", methods=["POST"])
def api_search() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        channels = search_channels(query)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"channels": channels})


@app.route("/api/podcast/search", methods=["POST"])
def api_podcast_search() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        shows = search_podcasts(query)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"shows": shows})


@app.route("/api/podcast/episodes", methods=["POST"])
def api_podcast_episodes() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    feed = (data.get("feed_url") or "").strip()
    if not feed:
        return jsonify({"error": "feed_url is required"}), 400
    try:
        feed = _require_http_url(feed)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    page = _int_arg(data, "page", 1, lo=1)
    page_size = _int_arg(data, "page_size", PODCAST_PAGE_SIZE, lo=1, hi=5000)
    query = (data.get("query") or "").strip() or None
    try:
        result = fetch_episodes(feed, page=page, page_size=page_size, query=query)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    # Flag episodes already on disk so the UI can gray them out.
    have = manager.podcast_downloaded(result["podcast"], result["videos"])
    for v in result["videos"]:
        v["downloaded"] = v["video_id"] in have
    return jsonify(result)


@app.route("/api/scrape", methods=["POST"])
def api_scrape() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    raw = (data.get("url") or "").strip()
    page = _int_arg(data, "page", 1, lo=1)
    ignore_shorts = bool(data.get("ignore_shorts", True))
    sort = data.get("sort") or "newest"
    if not raw:
        return jsonify({"error": "url is required"}), 400
    # Validate before firing a long-running yt-dlp subprocess.
    try:
        kind, value = validate_source(raw)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if kind == "query":
        # Plain text — tell the client to disambiguate via channel search.
        return jsonify({"needs_search": True, "query": value})
    url = value
    filters = {
        "query": (data.get("query") or "").strip() or None,
        "duration_min": _parse_int(data, "duration_min"),
        "duration_max": _parse_int(data, "duration_max"),
        "views_min": _parse_int(data, "views_min"),
        "views_max": _parse_int(data, "views_max"),
        "date_from": _norm_date(data.get("date_from")),
        "date_to": _norm_date(data.get("date_to")),
    }
    try:
        result = scrape_channel(url, page, SCRAPE_PAGE_SIZE, ignore_shorts=ignore_shorts,
                                filters=filters, sort=sort)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    result["resolved_url"] = url
    # Flag videos already archived locally so the UI can gray them out.
    downloaded = manager.downloaded_ids()
    for v in result.get("videos", []):
        v["downloaded"] = v.get("video_id") in downloaded
    if page == 1:
        manager.push_history(url)
    return jsonify(result)


@app.route("/api/tools/update-ytdlp", methods=["POST"])
def api_update_ytdlp() -> Response:
    if not _ytdlp_available():
        return jsonify({"error": "yt-dlp is not installed"}), 400
    # A module install updates via pip; the standalone binary self-updates.
    if _ytdlp_is_module():
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
    else:
        cmd = list(_ytdlp_base()) + ["-U"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "yt-dlp update timed out"}), 504
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return jsonify({
        "ok": proc.returncode == 0,
        "output": output[-2000:],
        "version": _ytdlp_version(),
    })


@app.route("/api/queue", methods=["GET"])
def api_queue() -> Response:
    return jsonify({
        "items": manager.snapshot(),
        "running": manager.is_running(),
        "download_dir": str(manager.download_dir),
    })


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    videos = data.get("videos") or []
    quality = data.get("quality") or "1080p"
    sponsorblock = data.get("sponsorblock") or []
    sb_mode = data.get("sponsorblock_mode") or "remove"
    source = data.get("source") or "youtube"
    if not isinstance(videos, list) or not videos:
        return jsonify({"error": "videos must be a non-empty list"}), 400
    try:
        added = manager.add(videos, quality, sponsorblock, sb_mode=sb_mode, source=source)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"added": [asdict(i) for i in added], "count": len(added)})


@app.route("/api/queue/log", methods=["GET"])
def api_queue_log() -> Response:
    item_id = request.args.get("id")
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    log = manager.get_log(item_id)
    status = next((it["status"] for it in manager.snapshot() if it["id"] == item_id), None)
    if log is None:
        return jsonify({"lines": [], "available": False, "status": status})
    return jsonify({"lines": log, "available": True, "status": status})


@app.route("/api/queue/remove", methods=["POST"])
def api_queue_remove() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    item_id = data.get("id")
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    ok = manager.remove(item_id)
    return jsonify({"ok": ok})


@app.route("/api/queue/retry", methods=["POST"])
def api_queue_retry() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    item_id = data.get("id")
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    ok = manager.retry(item_id)
    return jsonify({"ok": ok})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear() -> Response:
    removed = manager.clear_finished()
    return jsonify({"removed": removed})


@app.route("/api/queue/start", methods=["POST"])
def api_queue_start() -> Response:
    manager.start()
    return jsonify({"running": True})


@app.route("/api/queue/pause", methods=["POST"])
def api_queue_pause() -> Response:
    manager.pause()
    return jsonify({"running": False})


@app.route("/api/queue/cancel", methods=["POST"])
def api_queue_cancel() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    item_id = data.get("id")
    if item_id:
        cancelled = manager.cancel_item(item_id)
        return jsonify({"cancelled": bool(cancelled)})
    cancelled = manager.cancel_current()
    return jsonify({"cancelled": cancelled})


@app.route("/api/queue/pause-item", methods=["POST"])
def api_queue_pause_item() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    item_id = data.get("id")
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    ok = manager.pause_item(item_id)
    return jsonify({"ok": ok})


@app.route("/api/queue/resume-item", methods=["POST"])
def api_queue_resume_item() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    item_id = data.get("id")
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    ok = manager.resume_item(item_id)
    return jsonify({"ok": ok})


@app.route("/api/queue/reveal", methods=["POST"])
def api_queue_reveal() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    item_id = data.get("id")
    target: Path | None = None
    if item_id:
        for it in manager.snapshot():
            if it["id"] == item_id and it.get("output_file"):
                p = Path(str(it["output_file"]))
                if p.exists():
                    target = p
                    break
    if target is None:
        target = manager.download_dir
    try:
        _reveal_path(target)
    except (FileNotFoundError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "path": str(target)})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings() -> Response:
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        if "download_dir" in data and data["download_dir"]:
            try:
                manager.set_download_dir(data["download_dir"])
            except Exception as exc:
                return jsonify({"error": str(exc)}), 400
        if "concurrency" in data:
            try:
                manager.set_concurrency(int(data["concurrency"]))
            except (TypeError, ValueError):
                return jsonify({"error": "concurrency must be an integer"}), 400
        if any(k in data for k in ("embed_metadata", "embed_thumbnail", "embed_chapters")):
            cur = manager.settings()
            manager.set_embed(
                metadata=bool(data.get("embed_metadata", cur["embed_metadata"])),
                thumbnail=bool(data.get("embed_thumbnail", cur["embed_thumbnail"])),
                chapters=bool(data.get("embed_chapters", cur["embed_chapters"])),
            )
        if "use_archive" in data:
            manager.set_use_archive(bool(data["use_archive"]))
        if any(k in data for k in ("subtitles", "subtitle_langs", "subtitle_auto", "subtitle_embed")):
            cur = manager.settings()
            manager.set_subtitles(
                enabled=bool(data.get("subtitles", cur["subtitles"])),
                langs=data.get("subtitle_langs") if "subtitle_langs" in data else None,
                auto=bool(data.get("subtitle_auto", cur["subtitle_auto"])),
                embed=bool(data.get("subtitle_embed", cur["subtitle_embed"])),
            )
        if "cookies_browser" in data or "cookies_file" in data:
            manager.set_cookies(
                browser=data.get("cookies_browser") if "cookies_browser" in data else None,
                file=data.get("cookies_file") if "cookies_file" in data else None,
            )
        if any(k in data for k in ("last_quality", "last_sponsorblock", "last_sb_mode")):
            manager.remember_choices(
                data.get("last_quality"),
                data.get("last_sponsorblock") if isinstance(data.get("last_sponsorblock"), list) else None,
                data.get("last_sb_mode"),
            )
    settings = manager.settings()
    settings["categories"] = sorted(SPONSORBLOCK_CATEGORIES)
    settings["sort_orders"] = list(SORT_ORDERS)
    settings["qualities"] = {
        key: {"label": preset["label"], "audio_only": preset.get("audio_only", False)}
        for key, preset in QUALITY_PRESETS.items()
    }
    return jsonify(settings)


@app.route("/api/events")
def api_events() -> Response:
    listener = manager.listen()

    def stream():
        snapshot = json.dumps({
            "type": "snapshot",
            "items": manager.snapshot(),
            "running": manager.is_running(),
        })
        yield f"data: {snapshot}\n\n".encode("utf-8")
        try:
            while True:
                try:
                    payload = listener.get(timeout=15)
                    yield f"data: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    yield b": keepalive\n\n"
        finally:
            manager.drop(listener)

    # No "Connection" header: it is hop-by-hop, and waitress (PEP 3333) rejects
    # it with an AssertionError — which killed the stream before it opened.
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    response = Response(stream(), mimetype="text/event-stream", headers=headers)
    response.direct_passthrough = True
    return response


def main() -> None:
    host = _env("HOST") or "127.0.0.1"
    port = int(_env("PORT") or "8765")
    print(f"\nChive {__version__} 🌱  →  http://{host}:{port}")
    print(f"State file:       {STATE_FILE}")
    if _ytdlp_runner_cmd() is not None:
        print(f"yt-dlp:           API runner ({_ytdlp_version() or '?'}) — progress streams")
    elif _ytdlp_available():
        print(f"yt-dlp:           {(_ytdlp_base() or [''])[0]} ({_ytdlp_version() or '?'}) — "
              "standalone binary; progress may buffer on Windows. `pip install yt-dlp` for live progress.")
    else:
        print("yt-dlp:           NOT FOUND — install it (pip install yt-dlp)")
    if DEBUG:
        print(f"Debug logs:       {DOWNLOAD_LOG_DIR}  (raw yt-dlp output, timestamped)")
    print()

    # Stop running downloads on every exit path (Ctrl+C, SIGTERM, normal
    # exit). They run in their own process group, so they don't receive the
    # console's Ctrl+C and would otherwise outlive the server.
    atexit.register(manager.shutdown)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # Prefer the waitress WSGI server: it serves concurrent requests reliably
    # while a long-lived SSE stream is open (the Flask dev server can drop
    # requests mid-download, surfacing as a "NetworkError" on the log fetch).
    # Fall back to the dev server if waitress isn't installed.
    try:
        try:
            from waitress import serve
        except ImportError:
            print("(waitress not installed — using the Flask dev server; "
                  "`pip install -r requirements.txt` for more robust serving)\n")
            app.run(host=host, port=port, threaded=True, debug=False)
            return
        # `threads` caps concurrent workers; each open SSE connection holds one
        # for its lifetime, so allow headroom for multiple tabs + requests.
        serve(app, host=host, port=port, threads=16, channel_timeout=300, ident="Chive")
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping downloads…")
        manager.shutdown()


if __name__ == "__main__":
    main()
