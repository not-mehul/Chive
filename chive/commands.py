"""Build the yt-dlp download command for a queue item.

Pure: takes the item and a snapshot of the user's settings, returns argv.
"""

from __future__ import annotations

from . import config, ytdlp
from .library import podcast_filename, safe_segment
from .models import QueueItem
from .progress import PROGRESS_TEMPLATE
from .settings import Settings


def _prefix(out_template: str) -> list[str]:
    """Shared invocation: the flushing API runner when available, else the
    binary + --progress-template. Both emit the same [YTPROG] lines."""
    runner = ytdlp.runner_cmd()
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


def youtube_command(item: QueueItem, s: Settings) -> list[str]:
    preset = config.QUALITY_PRESETS.get(item.quality) or config.QUALITY_PRESETS[config.DEFAULT_QUALITY]
    audio_only = bool(preset.get("audio_only"))
    cmd = _prefix(str(s.download_dir / "%(uploader)s/%(title)s [%(id)s].%(ext)s"))
    cmd += ["-f", preset["format"]]
    if audio_only:
        cmd += ["-x", "--audio-format", preset["audio_format"]]
    elif "merge_output_format" in preset:
        cmd += ["--merge-output-format", preset["merge_output_format"]]
    if item.sponsorblock:
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
    if (s.embed_chapters or item.sponsorblock_mode == "mark") and not audio_only:
        cmd += ["--embed-chapters"]
    if s.use_archive:
        cmd += ["--download-archive", str(s.download_dir / "archive.txt")]
    return cmd + _cookies(s) + [item.url]


def podcast_command(item: QueueItem, s: Settings) -> list[str]:
    # An episode is a plain audio enclosure: downloaded as published (no
    # format selection / SponsorBlock) into <dir>/<show>/<title> [<tag>].<ext>.
    folder = s.download_dir / safe_segment(item.collection or "Podcasts")
    cmd = _prefix(str(folder / podcast_filename(item.title, item.video_id, "%(ext)s")))
    if s.embed_metadata:
        cmd += ["--embed-metadata"]
    return cmd + _cookies(s) + [item.url]


def build_command(item: QueueItem, s: Settings) -> list[str]:
    if item.source == "podcast":
        return podcast_command(item, s)
    return youtube_command(item, s)
