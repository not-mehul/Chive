"""Locating and invoking yt-dlp (and probing ffmpeg).

Downloads run through `ytdlp_runner.py`, which drives yt-dlp via its Python
API and emits progress from a flushing hook — the only reliable way to stream
progress on Windows, where yt-dlp's own stdout output is buffered. The plain
invocation (`base_cmd`) is used for scraping, search and the binary fallback.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from .config import RUNNER_PATH

# Tool versions only change on an install/update, but probing them spawns a
# process (~0.5 s for yt-dlp). Cache them; `invalidate_versions` after updating.
_VERSION_TTL = 600
_versions: dict[str, tuple[float, str | None]] = {}
_versions_lock = threading.Lock()


def module_available() -> bool:
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):
        return False


def base_cmd() -> list[str] | None:
    """Command prefix that runs yt-dlp, or None if it is unavailable."""
    if module_available():
        return [sys.executable, "-m", "yt_dlp"]
    binary = shutil.which("yt-dlp")
    return [binary] if binary else None


def available() -> bool:
    return base_cmd() is not None


def source() -> str | None:
    """"module" (streams progress reliably), "binary", or None if missing."""
    if module_available():
        return "module"
    return "binary" if shutil.which("yt-dlp") else None


def runner_cmd() -> list[str] | None:
    """The flushing API wrapper, or None when the module isn't importable
    (downloads then fall back to the binary + --progress-template)."""
    if RUNNER_PATH.exists() and module_available():
        return [sys.executable, str(RUNNER_PATH)]
    return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _probe(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip().splitlines()[0]
    return None


def _cached(key: str, compute) -> str | None:
    now = time.time()
    with _versions_lock:
        hit = _versions.get(key)
        if hit and now - hit[0] < _VERSION_TTL:
            return hit[1]
    value = compute()
    with _versions_lock:
        _versions[key] = (now, value)
    return value


def version() -> str | None:
    def compute():
        base = base_cmd()
        return _probe(base + ["--version"]) if base else None
    return _cached("yt-dlp", compute)


def ffmpeg_version() -> str | None:
    def compute():
        if not ffmpeg_available():
            return None
        raw = _probe(["ffmpeg", "-version"]) or ""
        return raw.replace("ffmpeg version ", "")[:24] or None
    return _cached("ffmpeg", compute)


def invalidate_versions() -> None:
    with _versions_lock:
        _versions.clear()


def run_json_lines(args: list[str], timeout: int, what: str) -> list[dict[str, Any]]:
    """Run yt-dlp with `args` (expected to print one JSON object per line) and
    return the parsed objects. Raises RuntimeError with yt-dlp's last error
    line if it failed without producing output."""
    base = base_cmd()
    if base is None:
        raise RuntimeError("yt-dlp is not installed or not on PATH")
    try:
        proc = subprocess.run(base + args, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out after {timeout}s") from None
    if proc.returncode != 0 and not proc.stdout.strip():
        lines = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"{what} failed")
    entries: list[dict[str, Any]] = []
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def update_command() -> list[str] | None:
    """How to update yt-dlp: pip for a module install, -U for the binary."""
    if module_available():
        return [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
    base = base_cmd()
    return base + ["-U"] if base else None


def explain_update_failure(output: str) -> str | None:
    """A plain-language hint for well-known update failures."""
    low = output.lower()
    if "externally-managed-environment" in low:
        return ("This Python is managed by your system (PEP 668), so pip won't "
                "update it. Run Chive from a virtual environment "
                "(python -m venv .venv) and install requirements there.")
    if "permission denied" in low or "access is denied" in low:
        return ("No permission to update yt-dlp in this location. Update it "
                "from a terminal, or run Chive from a virtual environment.")
    return None
