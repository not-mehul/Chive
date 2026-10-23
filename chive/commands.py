"""Build the yt-dlp download command for a queue item.

Pure: takes the item and a snapshot of the user's settings, returns argv.
Music and podcast items carry Chive's own tags (`--chive-tags`, applied by
ytdlp_runner.py); without the runner they fall back to yt-dlp's embedding.
"""

from __future__ import annotations

import json

from . import config, ytdlp
from .library import podcast_filename, safe_segment
from .models import QueueItem
from .progress import PROGRESS_TEMPLATE
from .settings import Settings


def _prefix(out_template: str, runner: list[str] | None) -> list[str]:
    """Shared invocation: the flushing API runner when available, else the
    binary + --progress-template. Both emit the same [YTPROG] lines."""
    cmd = list(runner) if runner else list(ytdlp.base_cmd() or ["yt-dlp"])
    cmd += ["--newline", "--no-colors", "--no-playlist"]
    if runner is None:
        cmd += ["--progress-template", PROGRESS_TEMPLATE]
    # --print implies --quiet, which would silence every status line we rely
    # on (archive skips, destinations, errors in context, the readable log);
    # --no-quiet undoes that while --print still reports the final path.
    cmd += ["-o", out_template, "--print", "after_move:filepath:%(filepath)s", "--no-quiet"]
    return cmd


def _cookies(s: Settings) -> list[str]:
    if s.cookies_browser:
        return ["--cookies-from-browser", s.cookies_browser]
    if s.cookies_file:
        return ["--cookies", s.cookies_file]
    return []


def _tags_arg(tags: dict) -> list[str]:
    return ["--chive-tags", json.dumps(tags, ensure_ascii=False, separators=(",", ":"))]


def video_command(item: QueueItem, s: Settings, out_template: str, sponsorblock: bool = True) -> list[str]:
    """YouTube and generic-web video/audio downloads."""
    preset = config.QUALITY_PRESETS.get(item.quality) or config.QUALITY_PRESETS[config.DEFAULT_QUALITY]
    audio_only = bool(preset.get("audio_only"))
    cmd = _prefix(out_template, ytdlp.runner_cmd())
    cmd += ["-f", preset["format"]]
    if audio_only:
        cmd += ["-x", "--audio-format", preset["audio_format"]]
    elif "merge_output_format" in preset:
        cmd += ["--merge-output-format", preset["merge_output_format"]]
    if sponsorblock and item.sponsorblock:
        flag = "--sponsorblock-mark" if item.sponsorblock_mode == "mark" else "--sponsorblock-remove"
        cmd += [flag, ",".join(item.sponsorblock)]
    # Subtitles: video presets only (can't embed into a bare audio file).
    # Embedding keeps captions in sync when segments are *removed* — yt-dlp's
    # ModifyChapters cuts embedded subtitle streams with the audio/video.
    # Sidecar .srt files are NOT re-timed, so embed is the default.
    if s.subtitles and not audio_only:
        cmd += ["--sub-langs", s.subtitle_langs or "en", "--write-subs"]
        if s.subtitle_auto:
            cmd += ["--write-auto-subs"]
        if s.subtitle_embed:
            cmd += ["--embed-subs"]
    if s.embed_metadata:
        cmd += ["--embed-metadata"]
    if s.embed_thumbnail:
        cmd += ["--embed-thumbnail"]
    # SponsorBlock "mark" needs chapters to be visible at all.
    if (s.embed_chapters or (sponsorblock and item.sponsorblock_mode == "mark")) and not audio_only:
        cmd += ["--embed-chapters"]
    if s.use_archive:
        cmd += ["--download-archive", str(s.download_dir / "archive.txt")]
    return cmd + _cookies(s) + [item.url]


def youtube_command(item: QueueItem, s: Settings) -> list[str]:
    return video_command(item, s, str(s.download_dir / "%(uploader)s/%(title)s [%(id)s].%(ext)s"))


def web_command(item: QueueItem, s: Settings) -> list[str]:
    # <dir>/Web/<site>/<uploader>/<title> [<id>].<ext>
    out = s.download_dir / config.WEB_DIR / "%(extractor_key)s" / \
        "%(uploader,channel,creator|Unknown)s" / "%(title)s [%(id)s].%(ext)s"
    return video_command(item, s, str(out), sponsorblock=False)


def music_folder(s: Settings, tags: dict) -> str:
    return str(s.download_dir / config.MUSIC_DIR / safe_segment(tags.get("album_artist") or tags.get("artist")
                                                                or "Unknown Artist")
               / safe_segment(tags.get("album") or "Unknown Album"))


def music_command(item: QueueItem, s: Settings) -> list[str]:
    """<dir>/Music/<artist>/<album>/: an album is downloaded whole, then split
    into "NN - Title" tracks; a track is saved as "NN - Title" directly."""
    fmt = config.MUSIC_FORMATS.get(item.quality) or config.MUSIC_FORMATS[config.DEFAULT_MUSIC_FORMAT]
    tags = dict(item.extra.get("tags") or {})
    mode = item.extra.get("music_mode", "album")
    folder = music_folder(s, tags)
    if mode == "album":
        name = safe_segment(tags.get("album") or item.title)
    else:
        title = safe_segment(tags.get("title") or item.title)
        name = f"{tags['track']:02d} - {title}" if tags.get("track") else title
    runner = ytdlp.runner_cmd()
    cmd = _prefix(f"{folder}/{name}.%(ext)s", runner)
    cmd += ["-f", fmt["format"], "-x", "--audio-format", fmt["audio_format"], "--audio-quality", "0"]
    if runner:
        cmd += _tags_arg({**tags, "mode": mode, "cover_url": item.thumbnail,
                          "cover_save": f"{folder}/cover.jpg", "square": True,
                          "keep_full": s.music_keep_full})
    else:
        # Binary fallback: yt-dlp's own tagging and splitting (all tracks then
        # share the album's tags — the runner path does this properly).
        cmd += ["--embed-metadata", "--embed-thumbnail"]
        if mode == "album":
            cmd += ["--split-chapters", "-o",
                    f"chapter:{folder}/%(section_number)02d - %(section_title)s.%(ext)s"]
    return cmd + _cookies(s) + [item.url]


def podcast_command(item: QueueItem, s: Settings) -> list[str]:
    # An episode is a plain audio enclosure: downloaded as published (no
    # format selection / SponsorBlock) into <dir>/<show>/<title> [<tag>].<ext>.
    folder = s.download_dir / safe_segment(item.collection or "Podcasts")
    runner = ytdlp.runner_cmd()
    cmd = _prefix(str(folder / podcast_filename(item.title, item.video_id, "%(ext)s")), runner)
    tags = item.extra.get("tags")
    if runner and tags and s.embed_metadata:
        artwork = item.extra.get("artwork")
        cover_url = item.thumbnail or artwork
        cmd += _tags_arg({**tags, "mode": "single", "cover_url": cover_url,
                          # The show's own artwork doubles as the folder cover
                          # (and the local RSS feed image); episode art is temporary.
                          "cover_save": str(folder / "cover.jpg") if cover_url == artwork else None,
                          "square": False})
    elif s.embed_metadata:
        cmd += ["--embed-metadata"]
    return cmd + _cookies(s) + [item.url]


_BUILDERS = {"podcast": podcast_command, "music": music_command, "web": web_command}


def build_command(item: QueueItem, s: Settings) -> list[str]:
    return _BUILDERS.get(item.source, youtube_command)(item, s)
