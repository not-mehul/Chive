"""The Flask app: JSON endpoints plus a Server-Sent Events stream.

Built by `create_app(manager)` so tests can run isolated instances.

Request guard: the bridge drives yt-dlp, writes files and runs pip on your
machine, so it must only obey its own page:
  * Host must be one we serve (blocks DNS-rebinding, where an attacker's
    domain is re-pointed at 127.0.0.1 to read our API).
  * State-changing requests must be JSON (browsers can't send that
    cross-site without a CORS preflight, which we never grant) and must not
    carry a foreign Origin.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__, config, podcasts, progress, youtube, ytdlp
from .manager import QueueManager
from .settings import API_FIELDS
from .util import disk_free, disk_total, reveal_path

# Some Windows registries map .js to text/plain, and browsers refuse to run an
# ES module served with a non-JavaScript type. Pin the types we serve.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def allowed_hosts_for(bind_host: str, extra: str | None = None) -> frozenset[str] | None:
    """Hostnames to accept. Bound to loopback → only loopback names (plus any
    in CHIVE_ALLOWED_HOSTS). Bound elsewhere → any, unless the list is set."""
    extras = {h.strip().lower() for h in (extra or "").split(",") if h.strip()}
    if bind_host in LOOPBACK_HOSTS:
        return LOOPBACK_HOSTS | extras
    return frozenset(extras) or None


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _int_arg(data: dict[str, Any], key: str, default: int,
             lo: int | None = None, hi: int | None = None) -> int:
    """An integer field; `default` when missing or malformed; clamped."""
    try:
        val = int(data.get(key)) if data.get(key) not in (None, "") else default
    except (TypeError, ValueError):
        val = default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def _opt_int(data: dict[str, Any], key: str) -> int | None:
    try:
        return int(data[key]) if data.get(key) not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def create_app(manager: QueueManager,
               allowed_hosts: frozenset[str] | None = LOOPBACK_HOSTS) -> Flask:
    app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")

    # ----- guard & errors ----------------------------------------------------

    @app.before_request
    def _guard():
        if allowed_hosts is not None:
            host = (urlparse("//" + (request.host or "")).hostname or "").lower()
            if host not in allowed_hosts:
                return _error(f"host '{host}' is not allowed (see CHIVE_ALLOWED_HOSTS)", 403)
        if request.path.startswith("/api/") and request.method not in _SAFE_METHODS:
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return _error("cross-origin request refused", 403)
            if not request.is_json:
                return _error("expected a JSON request (Content-Type: application/json)", 415)
        return None

    @app.errorhandler(Exception)
    def _api_error(exc: Exception):
        """API callers always get JSON, never an HTML traceback page."""
        if isinstance(exc, HTTPException):
            if not request.path.startswith("/api/"):
                return exc
            return _error(exc.description, exc.code or 500)
        app.logger.exception("unhandled error on %s", request.path)
        return _error(str(exc) or exc.__class__.__name__, 500)

    # ----- page & health -------------------------------------------------------

    @app.route("/")
    def index():
        return send_from_directory(config.STATIC_DIR, "index.html")

    @app.route("/api/health")
    def health():
        dl_dir = manager.download_dir
        free = disk_free(dl_dir)
        return jsonify({
            "ok": True,
            "version": __version__,
            "yt_dlp": ytdlp.available(),
            "yt_dlp_version": ytdlp.version(),
            "yt_dlp_source": ytdlp.source(),   # "module" streams reliably; "binary" may buffer
            "ffmpeg": ytdlp.ffmpeg_available(),
            "ffmpeg_version": ytdlp.ffmpeg_version(),
            "download_dir": str(dl_dir),
            "disk_free": free,
            "disk_total": disk_total(dl_dir),
            "disk_low": free is not None and free < config.DISK_SOFT_WARN,
            "disk_hard_min": config.DISK_HARD_MIN,
        })

    # ----- sources ---------------------------------------------------------------

    @app.post("/api/search")
    def api_search():
        query = (_body().get("query") or "").strip()
        if not query:
            return _error("query is required", 400)
        try:
            return jsonify({"channels": youtube.search_channels(query)})
        except RuntimeError as exc:
            return _error(str(exc), 502)

    @app.post("/api/scrape")
    def api_scrape():
        data = _body()
        raw = (data.get("url") or "").strip()
        if not raw:
            return _error("url is required", 400)
        try:
            kind, value = youtube.validate_source(raw)
        except ValueError as exc:
            return _error(str(exc), 400)
        if kind == "query":
            return jsonify({"needs_search": True, "query": value})
        page = _int_arg(data, "page", 1, lo=1)
        filters = {
            "query": (data.get("query") or "").strip() or None,
            "duration_min": _opt_int(data, "duration_min"),
            "duration_max": _opt_int(data, "duration_max"),
            "views_min": _opt_int(data, "views_min"),
            "views_max": _opt_int(data, "views_max"),
            "date_from": youtube.norm_date(data.get("date_from")),
            "date_to": youtube.norm_date(data.get("date_to")),
        }
        try:
            result = youtube.scrape_channel(
                value, page, config.SCRAPE_PAGE_SIZE,
                ignore_shorts=bool(data.get("ignore_shorts", True)),
                filters=filters, sort=data.get("sort") or "newest")
        except RuntimeError as exc:
            return _error(str(exc), 502)
        result["resolved_url"] = value
        have = manager.downloaded_ids()
        for v in result.get("videos", []):
            v["downloaded"] = v.get("video_id") in have
        if page == 1:
            manager.push_history(value)
        return jsonify(result)

    @app.post("/api/podcast/search")
    def api_podcast_search():
        query = (_body().get("query") or "").strip()
        if not query:
            return _error("query is required", 400)
        try:
            return jsonify({"shows": podcasts.search_podcasts(query)})
        except RuntimeError as exc:
            return _error(str(exc), 502)

    @app.post("/api/podcast/episodes")
    def api_podcast_episodes():
        data = _body()
        try:
            feed = podcasts.require_http_url(data.get("feed_url") or "")
        except ValueError as exc:
            return _error(str(exc), 400)
        try:
            result = podcasts.fetch_episodes(
                feed,
                page=_int_arg(data, "page", 1, lo=1),
                page_size=_int_arg(data, "page_size", config.PODCAST_PAGE_SIZE,
                                   lo=1, hi=config.PODCAST_MAX_PAGE_SIZE),
                query=(data.get("query") or "").strip() or None)
        except RuntimeError as exc:
            return _error(str(exc), 502)
        have = manager.podcast_downloaded(result["podcast"], result["videos"])
        for v in result["videos"]:
            v["downloaded"] = v["video_id"] in have
        return jsonify(result)

    # ----- queue -----------------------------------------------------------------

    @app.get("/api/queue")
    def api_queue():
        return jsonify({"items": manager.snapshot(), "running": manager.is_running(),
                        "download_dir": str(manager.download_dir)})

    @app.post("/api/queue/add")
    def api_queue_add():
        data = _body()
        videos = data.get("videos")
        if not isinstance(videos, list) or not videos:
            return _error("videos must be a non-empty list", 400)
        try:
            added = manager.add(
                videos, data.get("quality") or config.DEFAULT_QUALITY,
                data.get("sponsorblock") or [],
                sb_mode=data.get("sponsorblock_mode") or "remove",
                source=data.get("source") or "youtube")
        except ValueError as exc:
            return _error(str(exc), 400)
        return jsonify({"added": [asdict(i) for i in added], "count": len(added)})

    @app.get("/api/queue/log")
    def api_queue_log():
        item_id = request.args.get("id")
        if not item_id:
            return _error("id is required", 400)
        log = manager.get_log(item_id)
        item = manager.item(item_id)
        return jsonify({"lines": log or [], "available": log is not None,
                        "status": item["status"] if item else None})

    def _item_action(action):
        item_id = _body().get("id")
        if not item_id or not isinstance(item_id, str):
            return _error("id is required", 400)
        return jsonify({"ok": bool(action(item_id))})

    @app.post("/api/queue/remove")
    def api_queue_remove():
        return _item_action(manager.remove)

    @app.post("/api/queue/retry")
    def api_queue_retry():
        return _item_action(manager.retry)

    @app.post("/api/queue/cancel")
    def api_queue_cancel():
        return _item_action(manager.cancel_item)

    @app.post("/api/queue/pause-item")
    def api_queue_pause_item():
        return _item_action(manager.pause_item)

    @app.post("/api/queue/resume-item")
    def api_queue_resume_item():
        return _item_action(manager.resume_item)

    @app.post("/api/queue/clear")
    def api_queue_clear():
        return jsonify({"removed": manager.clear_finished()})

    @app.post("/api/queue/start")
    def api_queue_start():
        manager.start()
        return jsonify({"running": True})

    @app.post("/api/queue/pause")
    def api_queue_pause():
        manager.pause()
        return jsonify({"running": False})

    @app.post("/api/queue/reveal")
    def api_queue_reveal():
        item = manager.item(_body().get("id") or "")
        target: Path = manager.download_dir
        if item and item.get("output_file") and Path(item["output_file"]).exists():
            target = Path(item["output_file"])
        else:
            target.mkdir(parents=True, exist_ok=True)
        try:
            reveal_path(target)
        except (OSError, RuntimeError) as exc:
            return _error(str(exc), 400)
        return jsonify({"ok": True, "path": str(target)})

    # ----- settings & tools ----------------------------------------------------

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "POST":
            changes = {k: v for k, v in _body().items() if k in API_FIELDS}
            try:
                manager.update_settings(changes)
            except ValueError as exc:
                return _error(str(exc), 400)
        return jsonify({
            **manager.settings(),
            "max_concurrency": config.MAX_CONCURRENCY,
            "cookie_browsers": list(config.COOKIE_BROWSERS),
            "sb_modes": list(config.SPONSORBLOCK_MODES),
            "sponsorblock": [{"id": k, "label": v} for k, v in config.SPONSORBLOCK_LABELS.items()],
            "sort_orders": list(config.SORT_ORDERS),
            "indeterminate_phases": sorted(progress.INDETERMINATE_PHASES),
            "qualities": [
                {"id": k, "label": p["label"], "hint": p["hint"],
                 "audio_only": bool(p.get("audio_only"))}
                for k, p in config.QUALITY_PRESETS.items()
            ],
        })

    @app.post("/api/tools/update-ytdlp")
    def api_update_ytdlp():
        cmd = ytdlp.update_command()
        if cmd is None:
            return _error("yt-dlp is not installed", 400)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            return _error("yt-dlp update timed out", 504)
        except OSError as exc:
            return _error(str(exc), 500)
        ytdlp.invalidate_versions()
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        ok = proc.returncode == 0
        return jsonify({"ok": ok, "output": output[-2000:], "version": ytdlp.version(),
                        "hint": None if ok else ytdlp.explain_update_failure(output)})

    # ----- live events -----------------------------------------------------------

    @app.route("/api/events")
    def api_events():
        listener = manager.listen()

        def stream():
            snapshot = {"type": "snapshot", "items": manager.snapshot(),
                        "running": manager.is_running()}
            yield f"data: {json.dumps(snapshot, default=str)}\n\n".encode()
            try:
                while True:
                    try:
                        yield f"data: {listener.get(timeout=15)}\n\n".encode()
                    except queue.Empty:
                        yield b": keepalive\n\n"
            finally:
                manager.drop(listener)

        # No "Connection" header: it is hop-by-hop, and waitress (PEP 3333)
        # rejects it with an AssertionError, killing the stream before it opens.
        response = Response(stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        response.direct_passthrough = True
        return response

    return app
