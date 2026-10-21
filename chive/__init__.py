"""Chive (ar·chive) — archive YouTube channels and podcasts locally.

Snip the fluff, keep the good stuff. `server.py` at the repository root is the
entry point; this package holds the bridge:

    config     environment, paths and fixed constants
    settings   the user's persisted preferences (validated)
    models     the queue item record
    util       byte/time formatting, disk, process and filesystem helpers
    ytdlp      locating and invoking yt-dlp / ffmpeg
    progress   turning yt-dlp output lines into item progress and phases
    commands   building the yt-dlp download command for an item
    library    detecting what is already archived on disk
    youtube    channel scraping, search, filtering and sorting
    podcasts   podcast directory search and RSS feed parsing
    manager    the download queue: persistence, workers, logs, events
    api        the Flask app (JSON + Server-Sent Events)
"""

__version__ = "1.0.0"
