"""Run yt-dlp through its Python API with flushing progress hooks.

Why this exists: the standalone `yt-dlp.exe` — and even `python -m yt_dlp` —
buffer their progress output on Windows so it never reaches the parent process's
pipe until the very end, making downloads appear to jump straight from 0% to
100% with no live speed or ETA. Emitting progress from a `progress_hooks`
callback with an explicit `flush()` sidesteps all of that: the bytes leave this
process immediately, regardless of platform stdio buffering.

The server invokes this with the same yt-dlp CLI flags it would otherwise pass
to the binary; `yt_dlp.parse_options` turns them into the API options, so every
feature (formats, SponsorBlock, subtitles, cookies, archive, …) is preserved.

Progress is reported for the *whole item*: a "best video + best audio" pick is
two downloads, and per-stream progress would run 0→100% twice. A `before_dl`
step records the planned streams so each hook call can be folded into one
overall figure.

Lines emitted (each flushed, newline-terminated):
  [YTPROG] downloaded|total|estimate|speed|eta|status|ext
  [YTPP]   <postprocessor name>          (a postprocessing phase started)

Run by path (not as a package module), so it imports nothing from `chive`.
"""

from __future__ import annotations

import sys
from typing import Any

_SUBTITLE_EXTS = {"vtt", "srt", "ass", "ssa", "ttml", "srv1", "srv2", "srv3", "json3", "lrc"}
_PLAN_PP = "ChivePlan"


def _emit(line: str) -> None:
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def new_plan() -> dict[str, Any]:
    """Per-item stream plan: [(format_id, expected size)] and finished sizes."""
    return {"formats": [], "done": {}}


def progress_fields(d: dict[str, Any], plan: dict[str, Any]) -> tuple | None:
    """The [YTPROG] fields for one hook call — aggregated across the item's
    planned streams when there are several. None to skip (subtitle files)."""
    info = d.get("info_dict") or {}
    ext = info.get("ext")
    if ext in _SUBTITLE_EXTS:
        return None
    downloaded = d.get("downloaded_bytes")
    total, estimate = d.get("total_bytes"), d.get("total_bytes_estimate")
    speed, eta, status = d.get("speed"), d.get("eta"), d.get("status")
    raw = (downloaded, total, estimate, speed, eta, status, ext)

    formats = plan.get("formats") or []
    fid = info.get("format_id")
    ids = [f for f, _ in formats]
    if len(formats) < 2 or fid not in ids or downloaded is None:
        return raw
    idx = ids.index(fid)
    current = total or estimate
    if status == "finished":
        plan["done"][fid] = downloaded or current
    sizes = [plan["done"].get(f) or (current if f == fid else size) for f, size in formats]
    if not all(sizes):
        return raw   # a stream's size is unknown: per-stream progress is the best we have
    overall = float(sum(sizes))
    done = float(sum(sizes[:idx]) + downloaded)
    if speed:
        eta = max(0.0, (overall - done) / speed)
    if status == "finished" and idx < len(formats) - 1:
        status = "downloading"   # the item isn't finished until its last stream is
    return (done, overall, None, speed, eta, status, ext)


def main() -> int:
    import yt_dlp
    from yt_dlp.postprocessor.common import PostProcessor

    parsed = yt_dlp.parse_options(sys.argv[1:])
    opts = dict(parsed.ydl_opts)
    # The progress hook below is our single source of progress; suppress
    # yt-dlp's own progress line so it doesn't also spam the captured log.
    opts["noprogress"] = True
    plan = new_plan()

    class ChivePlanPP(PostProcessor):
        """Runs before the download with the full info dict — the only point
        where the list of streams to fetch (requested_formats) is visible."""

        def run(self, info):
            plan["formats"] = [
                (f.get("format_id"), f.get("filesize") or f.get("filesize_approx"))
                for f in info.get("requested_formats") or []
            ]
            plan["done"] = {}
            return [], info

    def progress_hook(d: dict) -> None:
        fields = progress_fields(d, plan)
        if fields is not None:
            _emit("[YTPROG] " + "|".join(str(v) for v in fields))

    def postprocessor_hook(d: dict) -> None:
        name = str(d.get("postprocessor") or "")
        if d.get("status") == "started" and name != _PLAN_PP:
            _emit("[YTPP] " + name)

    opts.setdefault("progress_hooks", []).append(progress_hook)
    opts.setdefault("postprocessor_hooks", []).append(postprocessor_hook)

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(ChivePlanPP(), when="before_dl")
        return int(ydl.download(parsed.urls) or 0)


if __name__ == "__main__":
    sys.exit(main())
