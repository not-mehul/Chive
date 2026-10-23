"""Chive — start the local bridge server.

    python server.py            → http://127.0.0.1:8765

Configuration is via environment variables (CHIVE_HOST, CHIVE_PORT,
CHIVE_STATE_DIR, CHIVE_DOWNLOAD_DIR, CHIVE_ALLOWED_HOSTS, CHIVE_DEBUG); see
the README. The application itself lives in the `chive` package.
"""

from __future__ import annotations

import atexit
import signal
import sys

from chive import __version__, config, ytdlp
from chive.api import allowed_hosts_for, create_app
from chive.manager import QueueManager
from chive.subscriptions import AutoSync


def _banner(host: str, port: int) -> None:
    print(f"\nChive {__version__} 🌱  →  http://{host}:{port}")
    print(f"State file:       {config.STATE_FILE}")
    if ytdlp.runner_cmd() is not None:
        print(f"yt-dlp:           API runner ({ytdlp.version() or '?'}) — progress streams")
    elif ytdlp.available():
        print(f"yt-dlp:           standalone binary ({ytdlp.version() or '?'}) — progress may "
              "buffer on Windows. `pip install yt-dlp` for live progress.")
    else:
        print("yt-dlp:           NOT FOUND — install it (pip install yt-dlp)")
    if config.DEBUG:
        print(f"Debug logs:       {config.DEBUG_LOG_DIR}  (raw yt-dlp output, timestamped)")
    print()


def main() -> None:
    host = config.env("HOST") or "127.0.0.1"
    try:
        port = int(config.env("PORT") or "8765")
    except ValueError:
        sys.exit("CHIVE_PORT must be a number")

    manager = QueueManager()
    app = create_app(manager, allowed_hosts_for(host, config.env("ALLOWED_HOSTS")))
    _banner(host, port)
    AutoSync(manager).start()     # checks followed podcasts when enabled in Settings

    # Stop running downloads on every exit path. They run in their own process
    # group, so they don't receive the console's Ctrl+C and would otherwise
    # outlive the server.
    atexit.register(manager.shutdown)
    if not config.IS_WINDOWS:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        try:
            from waitress import serve
        except ImportError:
            print("(waitress not installed — using the Flask dev server; "
                  "`pip install -r requirements.txt` for more robust serving)\n")
            app.run(host=host, port=port, threaded=True, debug=False)
            return
        # Prefer waitress: it serves concurrent requests reliably while the
        # long-lived SSE stream is open. Each open stream holds a thread for
        # its lifetime, so allow headroom for several tabs plus requests.
        serve(app, host=host, port=port, threads=16, channel_timeout=300, ident="Chive")
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping downloads…")
        manager.shutdown()


if __name__ == "__main__":
    main()
