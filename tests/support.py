"""Shared test helpers. Import this before any `chive` module: it points state
and downloads at throwaway directories so tests never touch real user data."""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="chive-test-"))
os.environ["CHIVE_STATE_DIR"] = str(_SANDBOX / "state")
os.environ["CHIVE_DOWNLOAD_DIR"] = str(_SANDBOX / "downloads")
os.environ.pop("CHIVE_DEBUG", None)
os.environ.pop("YTARCHIVE_DEBUG", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chive.manager import QueueManager  # noqa: E402
from chive.models import QueueItem  # noqa: E402

FAKE_YTDLP = str(Path(__file__).resolve().parent / "fake_ytdlp.py")


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(dir=_SANDBOX))


def make_item(**kw) -> QueueItem:
    base = dict(id="x", video_id="v", url="https://www.youtube.com/watch?v=v",
                title="t", thumbnail=None, duration=None, quality="1080p", sponsorblock=[])
    base.update(kw)
    return QueueItem(**base)


def make_manager(start_dispatcher: bool = False, **settings) -> QueueManager:
    """A private manager with its own state file, logs and download dir."""
    root = tmpdir()
    m = QueueManager(state_file=root / "state.json", item_log_dir=root / "logs",
                     start_dispatcher=start_dispatcher)
    m.update_settings({"download_dir": str(root / "dl"), **settings}, broadcast=False)
    return m


def fake_command(*args: str) -> list[str]:
    """argv for the scripted stand-in for yt-dlp (see fake_ytdlp.py)."""
    return [sys.executable, FAKE_YTDLP, *args]


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05):
    """Poll until predicate() is truthy; returns its value or raises."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout:.1f}s")


def status_of(manager: QueueManager, item_id: str) -> str | None:
    it = manager.item(item_id)
    return it["status"] if it else None


class MediaServer:
    """A local HTTP server for offline real downloads. /<name> serves bytes
    registered with add(); `delay` slows the transfer (seconds per 32 KiB)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.delay = 0.0
        files, server = self.files, self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def do_GET(self):
                data = files.get(self.path.lstrip("/"))
                if data is None:
                    self.send_error(404)
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0] or 0)
                self.send_response(206 if start else 200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data) - start))
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                try:
                    for i in range(start, len(data), 32768):
                        self.wfile.write(data[i:i + 32768])
                        if server.delay:
                            time.sleep(server.delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add(self, name: str, size: int) -> str:
        self.files[name] = os.urandom(size)
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

