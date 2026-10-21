"""The queue item record shared by the manager, command builder and parser."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import config

STATUSES = ("pending", "downloading", "paused", "completed", "failed", "cancelled")
ACTIVE_STATUSES = frozenset({"pending", "downloading", "paused"})
FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled"})
SOURCES = ("youtube", "podcast")


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
    sponsorblock_mode: str = "remove"
    source: str = "youtube"            # youtube | podcast
    collection: str | None = None      # podcast/show name → destination subfolder
    status: str = "pending"
    progress: float = 0.0
    speed: str | None = None
    eta: str | None = None
    message: str | None = None
    output_file: str | None = None
    # Numeric byte/rate tracking for the queue's aggregate stats; None until
    # the first measurable progress update.
    total_bytes: float | None = None
    downloaded_bytes: float | None = None
    speed_bps: float | None = None
    added_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def reset_transfer(self) -> None:
        """Clear the live transfer readouts (speed / eta / rate)."""
        self.speed = None
        self.eta = None
        self.speed_bps = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], default_quality: str) -> QueueItem:
        """Rebuild an item from persisted state, validating every field.
        Raises KeyError/TypeError/ValueError for an unusable record."""
        source = "podcast" if raw.get("source") == "podcast" else "youtube"
        mode = raw.get("sponsorblock_mode")
        status = raw.get("status")
        item = cls(
            id=str(raw.get("id") or uuid.uuid4()),
            video_id=str(raw["video_id"]),
            url=str(raw["url"]),
            title=str(raw.get("title") or raw["video_id"]),
            thumbnail=raw.get("thumbnail"),
            duration=raw.get("duration"),
            quality=str(raw.get("quality") or default_quality),
            sponsorblock=[c for c in (raw.get("sponsorblock") or [])
                          if c in config.SPONSORBLOCK_CATEGORIES],
            sponsorblock_mode=mode if mode in config.SPONSORBLOCK_MODES else "remove",
            source=source,
            collection=raw.get("collection"),
            status=status if status in STATUSES else "pending",
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
        # A download interrupted by a shutdown comes back as pending; it
        # resumes from its .part file when the queue next runs.
        if item.status == "downloading":
            item.status = "pending"
            item.reset_transfer()
            item.message = "Restored — will resume"
        return item
