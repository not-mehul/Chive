"""Shared test helpers. Import this before any `chive` module: it points state
and downloads at throwaway directories so tests never touch real user data."""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
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



HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_audio(path: Path, seconds: int = 30, ext: str = "mp3") -> Path:
    """A real (sine-wave) audio file, made with ffmpeg."""
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "64k"], "m4a": ["-c:a", "aac", "-b:a", "64k"]}[ext]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}", *codec, str(path)], check=True)
    return path


def make_image(path: Path, size: str = "640x360") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=1",
                    "-frames:v", "1", str(path)], check=True)
    return path


def probe(path) -> dict:
    """ffprobe: {"tags": {...lowercased}, "streams": [(type, codec, attached_pic)], "duration": s}."""
    import json
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:format_tags:stream=codec_type,codec_name:stream_disposition=attached_pic",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    return {"tags": {k.lower(): v for k, v in d["format"].get("tags", {}).items()},
            "duration": float(d["format"]["duration"]),
            "streams": [(st["codec_type"], st["codec_name"], st.get("disposition", {}).get("attached_pic"))
                        for st in d["streams"]]}
