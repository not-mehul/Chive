"""Chive (ar·chive) — archive YouTube channels and podcasts locally.

Snip the fluff, keep the good stuff. `server.py` at the repository root is the
entry point; this package holds the bridge:

    config         environment, paths and fixed constants
    settings       the user's persisted preferences (validated)
    models         the queue item record
    util           byte/time formatting, disk, process and filesystem helpers
    ytdlp          locating and invoking yt-dlp / ffmpeg
    ytdlp_runner   the flushing API wrapper (run as a subprocess)
    progress       turning yt-dlp output lines into item progress and phases
    commands       building the yt-dlp download command for an item
    tagging        tags, cover art and chapter-based album splitting
    library        file naming and already-downloaded detection
    catalog        the Library view: index, listing, safe access, delete
    feeds          local podcast RSS feeds and OPML
    youtube        channel scraping, search, filtering and sorting
    podcasts       podcast directory search and RSS feed parsing
    music          album / track lookup and metadata
    web            any-site lookup
    subscriptions  followed podcasts and auto-sync
    manager        the download queue: persistence, workers, logs, events
    api            the Flask app (JSON + Server-Sent Events)
"""

__version__ = "1.1.0"
