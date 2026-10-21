"""The download queue: persistence, worker pool, per-item logs and events.

Thread model: one dispatcher thread starts a worker thread per download (up
to the concurrency setting); `_lock` guards items, settings and process
bookkeeping. Every state change is broadcast to SSE listeners as JSON.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

from . import commands, config, library, progress
from .models import FINISHED_STATUSES, QueueItem
from .settings import Settings
from .util import (
    atomic_write_json,
    detached_popen_kwargs,
    disk_free,
    force_kill,
    human_bytes,
    load_json,
    terminate_proc,
)

_WINDOWS_CTRL_EXIT = (3221225786, -1073741510)   # 0xC000013A: CTRL_BREAK/CTRL_C
_LISTENER_BUFFER = 512
_DISK_SCAN_TTL = 30
_SAFE_ID_RE = re.compile(r"[\w-]{1,80}")


class QueueManager:
    def __init__(self, state_file: Path = config.STATE_FILE,
                 item_log_dir: Path = config.ITEM_LOG_DIR,
                 start_dispatcher: bool = True) -> None:
        self.state_file = state_file
        self.item_log_dir = item_log_dir
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()   # snapshot+write as one step
        self._wake = threading.Event()
        self._items: dict[str, QueueItem] = {}
        self._order: list[str] = []
        self._settings = Settings()
        self._running = False
        self._shutting_down = False
        self._active_procs: dict[str, subprocess.Popen] = {}
        self._dispatched: set[str] = set()
        self._pausing: set[str] = set()
        self._listeners: list[queue.Queue[str]] = []
        self._listeners_lock = threading.Lock()
        # Per-item log: an in-memory tail mirrored to <item_log_dir>/<id>.log.
        self._logs: dict[str, deque[str]] = {}
        self._log_files: dict[str, IO[str]] = {}
        self._dl_scan: dict[str, Any] | None = None
        self._load()
        if start_dispatcher:
            threading.Thread(target=self._dispatch_loop, daemon=True,
                             name="chive-dispatch").start()

    # ----- persistence ---------------------------------------------------

    def _load(self) -> None:
        data = load_json(self.state_file)
        self._settings = Settings.from_dict(data)
        for raw in data.get("queue") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = QueueItem.from_dict(raw, self._settings.last_quality)
            except (KeyError, TypeError, ValueError):
                continue
            if item.id in self._items:
                continue
            self._items[item.id] = item
            self._order.append(item.id)

    def _persist(self) -> None:
        with self._persist_lock:
            with self._lock:
                payload = {"version": config.STATE_VERSION, **self._settings.to_dict(),
                           "queue": [asdict(self._items[i]) for i in self._order]}
            try:
                atomic_write_json(self.state_file, payload)
            except OSError as exc:
                print(f"[chive] could not persist state: {exc}", file=sys.stderr)

    # ----- settings --------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return self._settings.to_dict()

    def settings_copy(self) -> Settings:
        with self._lock:
            return self._settings.copy()

    @property
    def download_dir(self) -> Path:
        with self._lock:
            return self._settings.download_dir

    def update_settings(self, changes: dict[str, Any], broadcast: bool = True) -> dict[str, Any]:
        """Validate and apply `changes`; returns what changed. Raises
        ValueError for a bad value (nothing is applied in that case)."""
        if changes.get("download_dir"):
            try:
                p = Path(str(changes["download_dir"]).strip()).expanduser().resolve()
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"download_dir: {exc}") from None
        with self._lock:
            trial = self._settings.copy()
            changed = trial.update(changes)     # raises before anything applies
            self._settings = trial
            if "download_dir" in changed:
                self._dl_scan = None
        if changed:
            if broadcast:
                self._broadcast({"type": "settings", **changed})
            self._persist()
            if "concurrency" in changed:
                self._wake.set()
        return changed

    def push_history(self, url: str) -> None:
        with self._lock:
            if not self._settings.push_history(url):
                return
            history = list(self._settings.history)
        self._broadcast({"type": "settings", "history": history})
        self._persist()

    # ----- queue mutation ----------------------------------------------------

    def add(self, videos: list[dict[str, Any]], quality: str | None,
            sponsorblock: list[str], sb_mode: str = "remove",
            source: str = "youtube") -> list[QueueItem]:
        source = "podcast" if source == "podcast" else "youtube"
        if source == "podcast":
            quality, clean_sb = config.PODCAST_QUALITY, []
        else:
            if quality not in config.QUALITY_PRESETS:
                raise ValueError(f"unknown quality preset: {quality}")
            clean_sb = [c for c in sponsorblock or [] if c in config.SPONSORBLOCK_CATEGORIES]
        sb_mode = sb_mode if sb_mode in config.SPONSORBLOCK_MODES else "remove"
        added: list[QueueItem] = []
        with self._lock:
            # Dedup against non-completed items: a failed/cancelled one should
            # be re-run via Retry; a completed one may be queued again.
            existing = {self._items[i].video_id for i in self._order
                        if self._items[i].status != "completed"}
            for v in videos:
                if not isinstance(v, dict):
                    continue
                vid = v.get("video_id") or v.get("id")
                if not vid or not isinstance(vid, str) or vid in existing:
                    continue
                url = v.get("url") or (f"https://www.youtube.com/watch?v={vid}"
                                       if source == "youtube" else None)
                if not url:
                    continue
                item = QueueItem(
                    id=str(uuid.uuid4()), video_id=vid, url=url,
                    title=v.get("title") or vid, thumbnail=v.get("thumbnail"),
                    duration=v.get("duration"), quality=quality,
                    sponsorblock=clean_sb, sponsorblock_mode=sb_mode, source=source,
                    collection=v.get("collection") if source == "podcast" else None,
                )
                self._items[item.id] = item
                self._order.append(item.id)
                existing.add(vid)
                added.append(item)
        if added:
            # One event per batch — queueing a 500-episode feed must not send
            # 500 events (each a full client re-render; overflowing buffers).
            self._broadcast({"type": "queued", "items": [asdict(i) for i in added]})
            if source == "youtube":
                self.update_settings({"last_quality": quality, "last_sponsorblock": clean_sb,
                                      "last_sb_mode": sb_mode}, broadcast=False)
            self._persist()
            self._wake.set()
        return added

    def remove(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item or item.status == "downloading":
                return False
            self._order.remove(item_id)
            del self._items[item_id]
            self._logs.pop(item_id, None)
        self._delete_log_file(item_id)
        self._broadcast({"type": "removed", "id": item_id})
        self._persist()
        return True

    def retry(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item or item.status not in {"failed", "cancelled"}:
                return False
            item.status = "pending"
            item.progress = 0.0
            item.reset_transfer()
            item.message = None
            item.started_at = item.finished_at = None
            # The failed attempt's log is kept; the next run appends below it.
            snap = asdict(item)
        self._broadcast({"type": "update", "item": snap})
        self._persist()
        self._wake.set()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            dropped = [i for i in self._order if self._items[i].status in FINISHED_STATUSES]
            for iid in dropped:
                del self._items[iid]
                self._logs.pop(iid, None)
            self._order = [i for i in self._order if i in self._items]
        for iid in dropped:
            self._delete_log_file(iid)
        if dropped:
            self._broadcast({"type": "cleared", "ids": dropped})
            self._persist()
        return len(dropped)

    def cancel_item(self, item_id: str) -> bool:
        with self._lock:
            proc = self._active_procs.get(item_id)
        return terminate_proc(proc) if proc is not None else False

    def pause_item(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            proc = self._active_procs.get(item_id)
            if proc is None:
                if item.status != "pending":
                    return False
                item.status = "paused"
                item.message = "Paused"
                snap = asdict(item)
            else:
                self._pausing.add(item_id)
        if proc is not None:
            return terminate_proc(proc)
        self._broadcast({"type": "update", "item": snap})
        self._persist()
        return True

    def resume_item(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item or item.status != "paused":
                return False
            # Keep the last percent so the bar holds its place until yt-dlp
            # reports progress off the .part file, instead of snapping to 0%.
            item.status = "pending"
            item.message = "Resuming"
            item.reset_transfer()
            item.finished_at = None
            snap = asdict(item)
        self._broadcast({"type": "update", "item": snap})
        self._persist()
        self._wake.set()
        return True

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

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(self._items[i]) for i in self._order]

    def item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            it = self._items.get(item_id)
            return asdict(it) if it else None

    # ----- already-downloaded detection ----------------------------------------

    def downloaded_ids(self) -> set[str]:
        """YouTube ids already archived: completed items, the download
        archive, and a cached scan of the destination for "[<id>]" files."""
        with self._lock:
            ids = {it.video_id for it in self._items.values() if it.status == "completed"}
            folder = self._settings.download_dir
            scan = self._dl_scan
        if scan and scan["dir"] == folder and time.time() - scan["ts"] < _DISK_SCAN_TTL:
            disk = scan["ids"]
        else:
            disk = library.scan_video_ids(folder)
            with self._lock:
                self._dl_scan = {"dir": folder, "ts": time.time(), "ids": disk}
        return ids | disk | library.archive_ids(folder)

    def podcast_downloaded(self, collection: str, episodes: list[dict[str, Any]]) -> set[str]:
        folder = self.download_dir / library.safe_segment(collection or "Podcasts")
        return library.podcast_downloaded(folder, episodes)

    # ----- per-item logs -------------------------------------------------------

    def _log_path(self, item_id: str) -> Path | None:
        # Ids are uuids we mint, but restored state is user-editable JSON —
        # never let an id become a path traversal.
        if not _SAFE_ID_RE.fullmatch(item_id or ""):
            return None
        return self.item_log_dir / f"{item_id}.log"

    def _open_item_log(self, item_id: str) -> None:
        path = self._log_path(item_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — closed in _close_item_log
        except OSError as exc:
            print(f"[chive] could not open item log: {exc}", file=sys.stderr)
            return
        with self._lock:
            old = self._log_files.pop(item_id, None)
            self._log_files[item_id] = fh
        if old is not None:
            old.close()

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
            path.unlink(missing_ok=True)

    def _log_line(self, item_id: str, line: str) -> None:
        with self._lock:
            buf = self._logs.setdefault(item_id, deque(maxlen=config.LOG_MAX_LINES))
            buf.append(line)
            fh = self._log_files.get(item_id)
            if fh is not None:
                try:
                    fh.write(line + "\n")
                    fh.flush()
                except (OSError, ValueError):
                    pass

    def get_log(self, item_id: str) -> list[str] | None:
        """The item's log — from memory this session, else from disk (logs
        outlive a restart). None when nothing was ever logged."""
        with self._lock:
            buf = self._logs.get(item_id)
            if buf is not None:
                return list(buf)
        path = self._log_path(item_id)
        if path is None or not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return [ln.rstrip("\n") for ln in deque(fh, maxlen=config.LOG_MAX_LINES)]
        except OSError:
            return None

    def _open_debug_log(self, item: QueueItem, cmd: list[str]) -> IO[str] | None:
        """With CHIVE_DEBUG, record every raw output line with a wall-clock
        offset, so streaming cadence (vs one end-of-run flush) is visible."""
        if not config.DEBUG:
            return None
        try:
            config.DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
            safe_vid = re.sub(r"[^\w.-]", "_", item.video_id)[:40]
            path = config.DEBUG_LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_vid}.log"
            fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 — closed by _process
            fh.write(f"# title: {item.title}\n# quality: {item.quality}\n")
            fh.write("# cmd: " + " ".join(cmd) + "\n")
            fh.write("# Each line is prefixed with seconds since the process started.\n\n")
            fh.flush()
            print(f"[chive] debug log → {path}", file=sys.stderr)
            return fh
        except OSError as exc:
            print(f"[chive] could not open debug log: {exc}", file=sys.stderr)
            return None

    # ----- SSE listeners -------------------------------------------------------

    def listen(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=_LISTENER_BUFFER)
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
                # A slow listener (background tab): evict the OLDEST event so
                # the newest survives — crucially terminal status updates,
                # whose loss would leave a row stuck mid-download.
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass

    # ----- dispatcher + workers ------------------------------------------------

    def _next_item(self) -> QueueItem | None:
        """Claim the next pending item if a slot is free (caller holds lock)."""
        if not self._running or self._shutting_down:
            return None
        if len(self._active_procs) + len(self._dispatched) >= self._settings.concurrency:
            return None
        for iid in self._order:
            it = self._items[iid]
            if it.status == "pending" and iid not in self._active_procs and iid not in self._dispatched:
                self._dispatched.add(iid)
                return it
        return None

    def _dispatch_loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    item = self._next_item()
                if item is None:
                    break
                threading.Thread(target=self._process, args=(item,), daemon=True,
                                 name=f"chive-dl-{item.id[:8]}").start()

    def build_command(self, item: QueueItem) -> list[str]:
        return commands.build_command(item, self.settings_copy())

    def _process(self, item: QueueItem) -> None:
        self._begin_item_log(item)
        # Refuse to start when critically low on space — a failed write
        # halfway through is worse than not starting.
        free = disk_free(self.download_dir)
        if free is not None and free < config.DISK_HARD_MIN:
            self._broadcast({"type": "disk", "free_bytes": free, "low": True})
            self._fail_before_start(item, f"Insufficient disk space ({human_bytes(free)} free)")
            return

        with self._lock:
            item.status = "downloading"
            item.started_at = time.time()
        self._broadcast({"type": "update", "item": asdict(item)})

        cmd = self.build_command(item)
        self._log_line(item.id, "$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace", **detached_popen_kwargs())
        except OSError as exc:
            msg = "yt-dlp not found on PATH" if isinstance(exc, FileNotFoundError) else str(exc)
            self._fail_before_start(item, msg)
            return
        with self._lock:
            self._dispatched.discard(item.id)
            if self._shutting_down:          # raced with shutdown(): no orphans
                force_kill(proc)
                proc.wait()
                proc.stdout.close()
                return
            self._active_procs[item.id] = proc

        outcome = self._consume_output(item, proc, cmd)
        rc = proc.wait()
        with self._lock:
            self._active_procs.pop(item.id, None)
            paused = item.id in self._pausing
            self._pausing.discard(item.id)
            shutting_down = self._shutting_down
        if shutting_down:
            # Leave the item as-is: it is restored as pending on the next
            # launch and resumes from its .part file.
            self._log_line(item.id, "[chive] server shutting down — will resume on next launch")
            self._close_item_log(item.id)
            return
        if paused:
            self._end_item(item, "paused", "Paused — partial file kept for resume")
        elif rc == 0 and outcome == progress.ARCHIVED:
            self._end_item(item, "completed", "Skipped — already in archive")
        elif rc == 0 and outcome == progress.ON_DISK:
            self._end_item(item, "completed", "Already on disk")
        elif rc == 0:
            self._end_item(item, "completed", "Saved")
        elif rc < 0 or (config.IS_WINDOWS and rc in _WINDOWS_CTRL_EXIT):
            self._end_item(item, "cancelled", "Stopped")
        else:
            self._end_item(item, "failed", item.message or f"yt-dlp exited {rc}")
        self._wake.set()

    def _consume_output(self, item: QueueItem, proc: subprocess.Popen, cmd: list[str]) -> str | None:
        """Stream the child's output into progress, events and the log.
        Returns ARCHIVED / ON_DISK when yt-dlp skipped the download."""
        outcome: str | None = None
        last_emit = 0.0
        last_sent: tuple[float, str | None] = (-1.0, None)
        logged_step = -1
        debug_fh = self._open_debug_log(item, cmd)
        t0 = time.time()
        assert proc.stdout is not None
        try:
            # readline() rather than iterating: iterator read-ahead can buffer
            # lines, surfacing progress as one 0%→100% jump at the end.
            for raw in iter(proc.stdout.readline, ""):
                line = raw.rstrip("\n").rstrip("\r")
                if debug_fh is not None:
                    try:
                        debug_fh.write(f"[+{time.time() - t0:8.3f}s] {line}\n")
                        debug_fh.flush()
                    except OSError:       # stop debugging; never stop reading
                        try:
                            debug_fh.close()
                        except OSError:
                            pass
                        debug_fh = None
                with self._lock:
                    kind = progress.apply_line(item, line)
                    pct = item.progress
                if kind in (progress.ARCHIVED, progress.ON_DISK):
                    outcome = kind
                if kind == progress.PROGRESS:
                    # Raw progress arrives many times a second; the log gets a
                    # readable summary every 10% instead.
                    step = int(pct // 10)
                    if step < logged_step:
                        logged_step = -1            # a new stream started
                    if step > logged_step:
                        logged_step = step
                        self._log_line(item.id, progress.summary(item))
                elif kind == progress.POSTPROCESS:
                    self._log_line(item.id, "[postprocess] " + line[6:].strip())
                else:
                    self._log_line(item.id, line)
                now = time.time()
                current = (item.progress, item.message)
                if current != last_sent and now - last_emit > 0.15:
                    self._broadcast({"type": "progress", "item": asdict(item)})
                    last_emit, last_sent = now, current
        except Exception as exc:  # keep the worker alive, but leave a trace
            self._log_line(item.id, f"[chive] output reader error: {exc!r}")
        finally:
            if debug_fh is not None:
                debug_fh.close()
            proc.stdout.close()           # otherwise every download leaks its pipe
        if (item.progress, item.message) != last_sent:
            # Let the bar reach its final value before the completion event.
            self._broadcast({"type": "progress", "item": asdict(item)})
        return outcome

    def _begin_item_log(self, item: QueueItem) -> None:
        """Open the log for a new attempt. Earlier attempts (a retry, a resume,
        a previous session) stay above a separator."""
        with self._lock:
            known = item.id in self._logs
        if not known:
            previous = self.get_log(item.id) or []
            with self._lock:
                self._logs.setdefault(item.id, deque(previous, maxlen=config.LOG_MAX_LINES))
        self._open_item_log(item.id)
        with self._lock:
            resuming = bool(self._logs[item.id])
        if resuming:
            self._log_line(item.id, "")
        self._log_line(item.id, f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} · "
                                f"{'resuming' if resuming else 'starting'}: {item.title}")

    def _fail_before_start(self, item: QueueItem, message: str) -> None:
        with self._lock:
            self._dispatched.discard(item.id)
        self._end_item(item, "failed", message)
        self._wake.set()

    def _end_item(self, item: QueueItem, status: str, message: str) -> None:
        self._log_line(item.id, f"=== {status}: {message}")
        self._close_item_log(item.id)
        with self._lock:
            item.status = status
            item.message = message
            item.finished_at = time.time()
            item.reset_transfer()
            if status == "completed":
                item.progress = 100.0
                if item.output_file:     # real size beats the estimate
                    try:
                        item.total_bytes = float(Path(item.output_file).stat().st_size)
                    except OSError:
                        pass
                if item.total_bytes:
                    item.downloaded_bytes = item.total_bytes
                self._dl_scan = None     # the library just changed
            snap = asdict(item)
        self._broadcast({"type": "update", "item": snap})
        self._persist()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop every running download before the server exits. Downloads run
        in their own process group, so they would otherwise outlive the server —
        and, restored as pending on the next launch, a second process would
        then write to the same .part file. Idempotent."""
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            self._running = False
            procs = list(self._active_procs.values())
        for proc in procs:
            terminate_proc(proc, escalate=False)
        deadline = time.time() + timeout
        for proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                force_kill(proc)
        self._persist()
