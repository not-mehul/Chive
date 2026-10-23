"""The user's persisted preferences, with validation in one place.

Every setting is declared once on `Settings`; its normaliser (in `_RULES`)
validates values from both the API and the state file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from . import config


class _Skip(Exception):
    """A value to silently ignore (e.g. an empty language list)."""


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _dir(v: Any) -> Path:
    if not isinstance(v, (str, Path)) or not str(v).strip():
        raise _Skip
    return Path(str(v).strip()).expanduser().resolve()


def _concurrency(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("concurrency must be an integer") from None
    return max(1, min(config.MAX_CONCURRENCY, n))


def _nonempty_str(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise _Skip
    return v.strip()


def _str(v: Any) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("expected a string")
    return v.strip()


def _cookie_browser(v: Any) -> str:
    b = (v or "").strip().lower() if isinstance(v, str) else ""
    return b if b in config.COOKIE_BROWSERS else ""


def _history(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise _Skip
    return [h for h in v if isinstance(h, str)][: config.HISTORY_MAX]


def _quality(v: Any) -> str:
    if v not in config.QUALITY_PRESETS:
        raise _Skip
    return v


def _sb_categories(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise _Skip
    return sorted({c for c in v if c in config.SPONSORBLOCK_CATEGORIES})


def _music_format(v: Any) -> str:
    if v not in config.MUSIC_FORMATS:
        raise _Skip
    return v


def _sync_hours(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("podcast_sync_hours must be an integer") from None
    if n not in config.PODCAST_SYNC_CHOICES:
        raise ValueError(f"podcast_sync_hours must be one of {config.PODCAST_SYNC_CHOICES}")
    return n


def _follows(v: Any) -> list[dict[str, Any]]:
    """Followed podcasts: [{feed_url, name, artwork, since}] — deduped by feed."""
    if not isinstance(v, list):
        raise _Skip
    out: dict[str, dict[str, Any]] = {}
    for f in v:
        if not isinstance(f, dict):
            continue
        feed = f.get("feed_url")
        if not isinstance(feed, str) or not feed.startswith(("http://", "https://")):
            continue
        out[feed] = {
            "feed_url": feed,
            "name": str(f.get("name") or feed)[:200],
            "artwork": f.get("artwork") if isinstance(f.get("artwork"), str) else None,
            "since": str(f.get("since") or "")[:8],
        }
    return list(out.values())


def _sb_mode(v: Any) -> str:
    if v not in config.SPONSORBLOCK_MODES:
        raise _Skip
    return v


_RULES: dict[str, Callable[[Any], Any]] = {
    "download_dir": _dir,
    "concurrency": _concurrency,
    "embed_metadata": _bool,
    "embed_thumbnail": _bool,
    "embed_chapters": _bool,
    "use_archive": _bool,
    "subtitles": _bool,
    "subtitle_langs": _nonempty_str,
    "subtitle_auto": _bool,
    "subtitle_embed": _bool,
    "cookies_browser": _cookie_browser,
    "cookies_file": _str,
    "history": _history,
    "last_quality": _quality,
    "last_sponsorblock": _sb_categories,
    "last_sb_mode": _sb_mode,
    "music_format": _music_format,
    "music_keep_full": _bool,
    "podcast_sync_hours": _sync_hours,
    "podcast_follows": _follows,
    "podcast_last_sync": lambda v: float(v),
}

# Settings the API may change directly. History is appended by scrapes and
# follows are managed through the podcast follow/unfollow/import endpoints.
API_FIELDS = frozenset(_RULES) - {"history", "podcast_follows", "podcast_last_sync"}


@dataclass
class Settings:
    download_dir: Path = field(default_factory=config.default_download_dir)
    concurrency: int = 1
    embed_metadata: bool = True
    embed_thumbnail: bool = True
    embed_chapters: bool = True
    use_archive: bool = False
    subtitles: bool = False
    subtitle_langs: str = "en"
    subtitle_auto: bool = False
    subtitle_embed: bool = True
    cookies_browser: str = ""
    cookies_file: str = ""
    history: list[str] = field(default_factory=list)
    last_quality: str = config.DEFAULT_QUALITY
    last_sponsorblock: list[str] = field(default_factory=lambda: ["selfpromo", "sponsor"])
    last_sb_mode: str = "remove"
    music_format: str = config.DEFAULT_MUSIC_FORMAT
    music_keep_full: bool = False      # keep the unsplit album file too
    podcast_sync_hours: int = 0        # auto-check followed shows (0 = off)
    podcast_follows: list[dict[str, Any]] = field(default_factory=list)
    podcast_last_sync: float = 0.0     # epoch seconds of the last auto-check

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Settings from persisted state; invalid or unknown values are dropped
        in favour of the defaults rather than failing the load."""
        s = cls()
        for key, rule in _RULES.items():
            if key in data:
                try:
                    setattr(s, key, rule(data[key]))
                except (_Skip, ValueError, TypeError, OSError):
                    pass
        return s

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply validated `changes`; returns the fields that actually changed,
        JSON-ready. Raises ValueError for a malformed value."""
        staged: dict[str, Any] = {}
        for key, value in changes.items():
            rule = _RULES.get(key)
            if rule is None:
                continue
            try:
                staged[key] = rule(value)
            except _Skip:
                continue
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}: {exc}") from None
        changed: dict[str, Any] = {}
        for key, value in staged.items():
            if getattr(self, key) != value:
                setattr(self, key, value)
                changed[key] = _jsonable(value)
        return changed

    def push_history(self, url: str) -> bool:
        url = (url or "").strip()
        if not url:
            return False
        new = ([url] + [h for h in self.history if h != url])[: config.HISTORY_MAX]
        if new == self.history:
            return False
        self.history = new
        return True

    def to_dict(self) -> dict[str, Any]:
        return {f.name: _jsonable(getattr(self, f.name)) for f in fields(self)}

    def copy(self) -> Settings:
        return replace(self, history=list(self.history),
                       last_sponsorblock=list(self.last_sponsorblock),
                       podcast_follows=[dict(f) for f in self.podcast_follows])


def _jsonable(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, list):
        return [dict(x) if isinstance(x, dict) else x for x in v]
    return v
