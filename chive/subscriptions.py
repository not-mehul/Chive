"""Followed podcasts: follow/unfollow, OPML, and syncing new episodes.

Following a show means "keep it archived from now on": a sync queues every
episode published since you followed it that isn't already downloaded or
queued. (The back catalogue stays one "Queue all" click away.) Syncs run on
demand, and optionally every N hours in the background.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

from . import feeds, podcasts
from .manager import QueueManager


def follows(manager: QueueManager) -> list[dict[str, Any]]:
    return manager.settings_copy().podcast_follows


def follow(manager: QueueManager, feed_url: str, name: str | None = None,
           artwork: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
    feed_url = podcasts.require_http_url(feed_url)
    current = [f for f in follows(manager) if f["feed_url"] != feed_url]
    current.append({"feed_url": feed_url, "name": name or feed_url, "artwork": artwork,
                    "since": since or time.strftime("%Y%m%d")})
    manager.update_settings({"podcast_follows": current})
    return follows(manager)


def unfollow(manager: QueueManager, feed_url: str) -> list[dict[str, Any]]:
    manager.update_settings({"podcast_follows": [f for f in follows(manager) if f["feed_url"] != feed_url]})
    return follows(manager)


def import_opml(manager: QueueManager, text: str) -> int:
    """Follow every show in an OPML file; returns how many were new."""
    shows = feeds.opml_import(text)
    known = {f["feed_url"] for f in follows(manager)}
    today = time.strftime("%Y%m%d")
    merged = follows(manager) + [{**s, "artwork": None, "since": today}
                                 for s in shows if s["feed_url"] not in known]
    manager.update_settings({"podcast_follows": merged})
    return len(merged) - len(known)


def export_opml(manager: QueueManager) -> str:
    return feeds.opml_export(follows(manager))


def new_episodes(feed: dict[str, Any], since: str, have: set[str]) -> list[dict[str, Any]]:
    """Episodes published on/after `since` (YYYYMMDD) not in `have`. Undated
    episodes are skipped — there's no telling whether they're new."""
    out = []
    for ep in feed["episodes"]:
        date = ep.get("upload_date") or ""
        if date and date >= (since or "") and ep["video_id"] not in have:
            out.append(ep)
    return out


def sync(manager: QueueManager, feed_url: str | None = None) -> dict[str, Any]:
    """Queue new episodes of one followed show (or all). Returns
    {"added": n, "shows": {name: n}, "errors": {name: message}}."""
    targets = [f for f in follows(manager) if feed_url in (None, f["feed_url"])]
    queued = {it["video_id"] for it in manager.snapshot() if it["status"] != "completed"}
    added_total, per_show, errors = 0, {}, {}
    for show in targets:
        try:
            feed = podcasts.load_feed(show["feed_url"], fresh=True)
        except RuntimeError as exc:
            errors[show["name"]] = str(exc)
            continue
        on_disk = manager.podcast_downloaded(feed["podcast"], feed["episodes"])
        fresh = new_episodes(feed, show.get("since") or "", on_disk | queued | manager.library.ids())
        if fresh:
            added = manager.add(fresh, None, [], source="podcast")
            per_show[show["name"]] = len(added)
            added_total += len(added)
    manager.update_settings({"podcast_last_sync": time.time()}, broadcast=False)
    return {"added": added_total, "shows": per_show, "errors": errors}


class AutoSync:
    """Background thread: syncs followed shows every `podcast_sync_hours`
    and starts the queue when something new was found."""

    def __init__(self, manager: QueueManager, tick: float = 60.0) -> None:
        self.manager = manager
        self.tick = tick

    def start(self) -> AutoSync:
        # A daemon thread: it simply ends with the server.
        threading.Thread(target=self._loop, daemon=True, name="chive-autosync").start()
        return self

    def due(self, now: float | None = None) -> bool:
        s = self.manager.settings_copy()
        if not s.podcast_sync_hours or not s.podcast_follows:
            return False
        return (now or time.time()) - s.podcast_last_sync >= s.podcast_sync_hours * 3600

    def run_once(self) -> dict[str, Any] | None:
        if not self.due():
            return None
        result = sync(self.manager)
        if result["added"]:
            self.manager.start()
            self.manager.notify(f"New podcast episodes: {result['added']} queued")
        return result

    def _loop(self) -> None:
        while True:
            time.sleep(self.tick)
            try:
                self.run_once()
            except Exception as exc:  # never let the thread die
                print(f"[chive] podcast auto-sync failed: {exc}", file=sys.stderr)
