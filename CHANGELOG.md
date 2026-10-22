# Changelog

All notable changes to Chive are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-10-22

### Added
- **Library view** — browse everything archived (indexed metadata merged with
  a scan of the download folder), search, filter by source, sort, play in the
  browser with seeking, save a copy, and delete (with subtitles/thumbnails and
  the download-archive entry).
- **Music** — album/playlist URLs (YouTube Music, Bandcamp, SoundCloud…) or
  album search. Full-album uploads are split by chapter into `NN - Title`
  tracks, each tagged (title, track n/N, album, artist, album artist, year)
  with square cover art; MP3 or M4A; optional "keep the full album".
- **Any site** — generic URL mode for anything yt-dlp supports, saved under
  `Web/<site>/<uploader>/`.
- **Podcast extras** — real episode tags and artwork from the feed; follow
  shows and check for new episodes on demand or every 1–24 hours; OPML import
  and export; local RSS feeds of archived shows for podcast apps.
- **Desktop notifications** (opt-in) for finished/failed downloads, a drained
  queue and new podcast episodes; download progress in the tab title.
- Self-hosted fonts — the page makes no third-party font requests.
- `/` focuses search; Archive/Library tabs (with `#library` links); reduced
  motion respected throughout.

### Security
- **Request guard**: the API now only obeys its own page. Requests must be
  addressed to a loopback host (blocks DNS rebinding), and state-changing
  requests must be JSON without a foreign `Origin` (blocks other websites from
  queueing downloads, changing settings or running the yt-dlp updater).
  `CHIVE_ALLOWED_HOSTS` adds hostnames for LAN setups.
- Library file access and deletion are confined to the download folder
  (paths re-validated on every request, symlinks out of it refused); request
  bodies are capped at 8 MB; OPML with DOCTYPE/ENTITY declarations is refused.

### Changed
- **Whole-item progress**: a video+audio download now reports one progress
  figure instead of running the bar 0→100% once per stream.
- **Default download folder** is `~/Downloads/Chive` (or `CHIVE_DOWNLOAD_DIR`),
  outside the app folder. An existing non-empty `downloads/` keeps being used.
- Health checks no longer spawn processes every 30 s — tool versions are
  cached (and refreshed after an update).
- Podcast feeds are cached, so paging and filtering a show no longer
  re-downloads the whole feed; the scrape cache is now bounded.
- Podcast episodes no longer carry a meaningless video-quality preset.
- Failed yt-dlp updates explain common causes (e.g. a system-managed Python).
- Dialogs are accessible: focus moves in, Tab stays in, Escape closes only
  the top dialog, and focus returns to what opened it.
- Completed rows show *how* they completed ("Skipped — already in archive").

### Internal
- `server.py` (2,400 lines) split into the `chive` package: config, settings,
  models, manager, commands, progress, ytdlp, youtube, podcasts, library, api.
  Settings are one validated dataclass instead of three hand-synced copies.
- Frontend split into ES modules (`static/js/`); the quality and SponsorBlock
  lists now come from the server instead of being duplicated in JS.
- Removed dead code: unused stats/cancel-all paths, a pass-through helper,
  unused CSS rules and the retired amber colour tokens.
- Tests: Python unit, API, real-process lifecycle and offline real-download
  tests (including real ffmpeg album splitting and tagging), JS unit tests,
  and a browser end-to-end test covering download → library → play → delete;
  ruff lint. CI runs all of it on Linux and Windows.

### Fixed
- **Live updates under waitress**: the event stream sent a hop-by-hop
  `Connection` header, which waitress rejects — the stream died before it
  opened, so the queue never populated or updated.
- **Log viewer**: logs are now written to disk (`<state dir>/item-logs`) and
  survive restarts; the viewer follows a running download live; progress is
  summarised every 10% instead of flooding the log; retries and resumes append
  below a separator so the earlier failure stays visible.
- **Archive skips reported as "Saved"**: `--print` silently implied `--quiet`,
  hiding yt-dlp's status lines. Skips now show "Skipped — already in archive"
  (or "Already on disk").
- **Same-titled podcast episodes** ("Trailer", "Bonus") overwrote/skipped each
  other. Files are now `<title> [<tag>].<ext>`, with the tag from the episode
  guid; older files still count as downloaded.
- **Downloads outliving the server**: quitting now stops running downloads;
  they resume from their `.part` file on the next launch.
- Queueing a whole feed sends one event instead of one per episode (the UI
  froze and long batches could drop items); unqueueing no longer rebuilds the
  whole queue per item.
- Malformed `page` values returned an HTML 500; API errors are now always
  JSON. Podcast feed URLs must be `http(s)` (no `file://`).
- Each download leaked its stdout pipe; concurrent state writes could land out
  of order; podcast guids with quotes broke card selection; "Download .log"
  after a yt-dlp update saved the wrong log; subtitle settings didn't sync
  across tabs.
- Docs/UI text: accurate privacy note, `CHIVE_DEBUG` description, and the
  cookies hint now points at Settings → Access & throughput.

## [1.0.0] — 2026-06-22

First public release. Chive (ar·chive) is a local web app for archiving
YouTube channels and podcasts — snip the fluff, keep the good stuff.

### YouTube
- Scrape a channel or playlist, or **search by name / `@handle`**, and browse
  it in pages of 50.
- **Click a thumbnail to queue it instantly**; queue or unqueue a whole page.
- **Filter and sort across the entire channel** — title search, duration,
  views, upload date, and six sort orders.
- **SponsorBlock**: remove segments (default) or mark them as chapters.
- **Subtitles**: download and embed captions, kept in sync across cuts.
- Quality presets (4K/1080p/720p, M4A/MP3), library embedding (metadata,
  thumbnail, chapters), cookies for age-restricted videos, and a download
  archive.
- Already-downloaded videos are detected and grayed out.

### Podcasts
- **Search podcasts** (Apple/iTunes directory) and browse a show's episodes.
- **Queue single episodes or the whole feed**; episodes download as-is into
  `<Podcast>/<Episode>`.
- Already-downloaded episodes are grayed out.

### Queue & engine
- Parallel downloads with live per-item progress (percent, speed, ETA) over
  Server-Sent Events, plus aggregate stats (remaining, speed, archived size).
- Pause / resume / cancel / retry, per-item log viewer, reveal-in-folder.
- Progress is driven through yt-dlp's Python API with a flushing hook, so the
  bar streams reliably — including on Windows, where the standalone binary
  buffers its output.
- Served by waitress for robust concurrent requests alongside the live stream.
- Disk-space guard, SSE reconnection indicator, persistent queue and settings.

### Design
- Editorial "Dusk / Dawn" theme with a chive-green + lavender palette, a sprig
  motif, and a few small easter eggs. Fully keyboard-accessible.

### Under the hood
- Three tiers: a Flask/waitress bridge (`server.py`), a no-build SPA
  (`static/`), and local `yt-dlp` + `ffmpeg`.
- Everything runs locally and binds to `127.0.0.1`; no telemetry.
- Offline unit-test suite (`tests/`).
