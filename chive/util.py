"""Formatting, disk, filesystem and process helpers."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .config import IS_WINDOWS


class TTLCache:
    """A small thread-safe cache: entries expire after `ttl` seconds and the
    least recently used is evicted beyond `maxsize`, so it can't grow without
    bound over a long-running session."""

    def __init__(self, ttl: float, maxsize: int) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if time.time() - hit[0] >= self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return hit[1]

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

# ----- formatting ------------------------------------------------------------


def human_bytes(n: float | None) -> str:
    if not n or n <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i else f"{int(f)} {units[i]}"


def fmt_eta(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ----- disk & files ----------------------------------------------------------


def disk_free(path: Path) -> int | None:
    """Free bytes on the filesystem holding `path` — or its nearest existing
    parent, since the destination may not have been created yet."""
    p = path
    for _ in range(32):
        try:
            return shutil.disk_usage(str(p)).free
        except (FileNotFoundError, NotADirectoryError):
            if p.parent == p:
                return None
            p = p.parent
        except OSError:
            return None
    return None


def disk_total(path: Path) -> int | None:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).total
    except OSError:
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON so a crash mid-write can never leave a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_json(path: Path) -> dict[str, Any]:
    """A JSON object from `path`, or {} if it is missing or unreadable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def reveal_path(target: Path) -> None:
    """Open the OS file manager focused on `target` (or its parent if a file)."""
    if not target.exists():
        raise FileNotFoundError(str(target))
    try:
        if IS_WINDOWS:
            arg = f"/select,{target}" if target.is_file() else str(target)
            subprocess.Popen(["explorer", arg])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)] if target.is_file() else ["open", str(target)])
        else:
            folder = target if target.is_dir() else target.parent
            subprocess.Popen(["xdg-open", str(folder)])
    except FileNotFoundError as exc:
        raise RuntimeError(f"could not open file manager: {exc}") from exc


# ----- processes -------------------------------------------------------------

TERMINATE_GRACE = 10  # seconds between the polite stop and SIGKILL


def detached_popen_kwargs() -> dict[str, Any]:
    """Start a child in its own process group, so stopping it can signal its
    whole tree (yt-dlp + ffmpeg). A detached group doesn't get the console's
    Ctrl+C — QueueManager.shutdown() stops it when the server exits."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def force_kill(proc: subprocess.Popen) -> None:
    if not IS_WINDOWS:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _escalate_after_grace(proc: subprocess.Popen) -> None:
    # A child that ignores the polite signal would otherwise leave its worker
    # blocked on wait() forever, permanently consuming a concurrency slot.
    try:
        proc.wait(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        force_kill(proc)


def terminate_proc(proc: subprocess.Popen, escalate: bool = True) -> bool:
    """Politely stop `proc` and its group; escalate to a kill after a grace
    period. Returns False if it had already exited."""
    if proc.poll() is not None:
        return False
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
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
