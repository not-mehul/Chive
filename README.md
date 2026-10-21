# Chive 🌱

**ar·chive** — snip the fluff, keep the good stuff.

![CI](https://github.com/not-mehul/ytarchive/actions/workflows/ci.yml/badge.svg)
![version](https://img.shields.io/badge/version-1.0.0-84c95c)
![license](https://img.shields.io/badge/license-MIT-blue)

A local web app for archiving **YouTube, podcasts, music and almost any video
site** — with SponsorBlock segments cut out, albums split into properly tagged
tracks, and a built-in library to browse and play it all. Everything runs on
your machine: no telemetry, no third-party fonts or scripts. The browser only
talks to Chive (plus thumbnail/artwork images from the sites you archive).

Built as a three-tier system:

- **Frontend** — a single-page UI in `static/` (HTML + CSS + JS, no build step).
- **Bridge** — a Python/Flask server in `server.py` (served by waitress) that
  talks to the UI and drives `yt-dlp`.
- **Engine** — local `yt-dlp` + `ffmpeg` installations on your `PATH`.

Everything routes through `http://127.0.0.1:8765`.

## Features

- **YouTube** — scrape a channel/playlist or search by name/`@handle`; click a
  thumbnail to queue; filter & sort the whole channel; SponsorBlock remove/mark;
  subtitles; quality presets; already-downloaded videos are grayed out.
- **Podcasts** — search shows, browse episodes, queue one or the whole feed.
  Episodes get real tags (show, host, date, artwork). **Follow** shows to keep
  them archived (auto-check every few hours, or on demand), **import/export
  OPML**, and subscribe to your archive as **local RSS feeds** from any podcast
  app.
- **Music** — paste a YouTube Music / Bandcamp / SoundCloud album or playlist,
  or search for an album. Full-album uploads are **split into one track per
  chapter**, each tagged with title, track n/N, album, artist and year, with
  square cover art — in `Music/<Artist>/<Album>/`.
- **Any site** — anything yt-dlp supports (Vimeo, Twitch VODs, news sites…),
  single videos or playlists, saved under `Web/<site>/`.
- **Library** — browse everything you've archived, search it, filter by
  source, play it in the browser (seekable), save a copy, or delete it.
- **Queue** — parallel downloads, live progress/speed/ETA (also in the tab
  title), aggregate stats, pause/resume/cancel/retry, live per-item logs,
  reveal-in-folder, and optional **desktop notifications**.
- **Design** — an editorial Dusk/Dawn theme, keyboard-accessible (press `/`
  to search), respects reduced motion, a few easter eggs.

## Requirements

| Tool       | Why it's needed                              |
| ---------- | -------------------------------------------- |
| `yt-dlp`   | scraping, downloading, SponsorBlock lookup   |
| `ffmpeg`   | muxing, cutting sponsor segments, tagging, splitting albums |
| `python3`  | runs the bridge server                       |

Install them via your package manager:

```sh
# macOS
brew install yt-dlp ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
pipx install yt-dlp   # or: pip install --user yt-dlp

# Windows (winget)
winget install yt-dlp.yt-dlp
winget install Gyan.FFmpeg
```

Confirm they resolve:

```sh
yt-dlp --version
ffmpeg -version
```

> **Windows: install yt-dlp with pip.** When the `yt_dlp` module is importable,
> Chive drives it through `ytdlp_runner.py` — the Python API plus a flushing
> progress hook — instead of the standalone `yt-dlp.exe`, whose frozen build
> buffers its progress output and never flushes it, making downloads appear to
> jump straight from 0% to 100% with no live speed or ETA. `pip install -r
> requirements.txt` (which includes `yt-dlp`) into the same environment that
> runs the server gives you live progress. The Settings → *Engine & storage*
> panel shows whether the **module** or **binary** is in use.

## Running

```sh
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 server.py
```

Then open <http://127.0.0.1:8765>.

### Configuration

All settings have sensible defaults; override any of them with environment
variables. `CHIVE_*` is the primary prefix; the legacy `YTARCHIVE_*` names
are still honored as a fallback.

| Variable            | Default       | What it does                          |
| ------------------- | ------------- | ------------------------------------- |
| `CHIVE_HOST`        | `127.0.0.1`   | interface the server binds to         |
| `CHIVE_PORT`        | `8765`        | port the server listens on            |
| `CHIVE_STATE_DIR`   | `~/.chive`    | where queue/settings/history/logs persist |
| `CHIVE_DOWNLOAD_DIR`| `~/Downloads/Chive` | first-run download folder (changeable in Settings) |
| `CHIVE_ALLOWED_HOSTS` | —           | extra hostnames to accept, comma-separated (see *Security*) |
| `CHIVE_DEBUG`       | off           | also write raw, timestamped yt-dlp output to `<state dir>/logs` |

```sh
CHIVE_HOST=127.0.0.1 CHIVE_PORT=8765 python3 server.py

# Windows (PowerShell):
$env:CHIVE_PORT="9000"; python server.py
```

> **Upgrading from ytarchive?** If a `~/.ytarchive` state directory already
> exists and `~/.chive` does not, Chive keeps using the old location so your
> queue and settings carry over untouched.

## Using it

1. In **Source**, paste a channel/playlist URL, an `@handle`, or just type
   a channel **name** to search and pick from the results. Hit **Fetch**.
   Videos are pulled in pages of 50.
2. **Click a thumbnail to queue it instantly** (click again to remove it).
   The page-actions let you queue or unqueue a whole page at once. Use the
   filter/sort bar to **search titles, filter by duration/views/date, and
   sort across the whole channel**.
3. Open **Settings** (the gear, top-right) to configure everything: SponsorBlock
   segments and **Remove** vs **Mark as chapters**, quality preset, **Subtitles**,
   library embedding, cookies, concurrency/archive, the **download path**, the
   yt-dlp/ffmpeg versions (with an **Update yt-dlp** button), and free disk.
4. Hit **Start** in the Queue section.
5. Watch live progress and the **aggregate stats** (remaining, speed, archived
   size, ETA). Each row has a **log** button to inspect the yt-dlp output — it
   follows a running download live and is kept across restarts;
   failed items can be diagnosed without re-running in a terminal.

### Music

Switch to **Music** and paste an album or playlist link, or type an album
name to search YouTube for full-album uploads. Playlists are queued as
individual tracks with their album position; a single full-album video is
downloaded once and **split by its chapters** into `NN - Title` tracks. Every
track is tagged (title, track n/N, album, artist, album artist, year) and gets
the album art — cropped square — embedded and saved as `cover.jpg`. Track
titles are cleaned of numbering and noise like "(Official Audio)". Choose MP3
or M4A in Settings; tick *Keep the full album file* to keep the unsplit file
too. (Splitting and tagging need `ffmpeg`.)

### Any site

**Any site** takes a link to anything yt-dlp supports — one video or a whole
playlist — and downloads it with your quality preset into
`Web/<site>/<uploader>/`.

### Library

The **Library** tab lists every media file in your download folder — with the
metadata Chive recorded when it finished (title, show/album, artist, track),
or described from file and folder names for anything older or added by hand.
Search, filter by source, sort, play in the browser (with seeking), save a
copy, or delete. Deleting also removes subtitles/thumbnails next to the file
and its entry in the download archive, so it can be downloaded again.

### Podcasts

Flip the **YouTube / Podcasts** toggle in the Source header to archive audio
podcasts with the same flow. Search a show by name (via the free Apple/iTunes
podcast directory), pick it, and its episodes load into the same grid — click
an episode to queue it, or **Queue all** to grab the entire feed. Episodes are
the show's RSS audio enclosures, downloaded as-is by yt-dlp's generic extractor
into `…/<Podcast>/<Episode> [<tag>].<ext>` (the tag comes from the episode's
feed id, so same-titled episodes such as "Trailer" never collide), reusing the whole queue/progress/stats
machinery. Already-downloaded episodes are grayed out, just like videos.

- **Tags** — each episode is tagged from the feed: title, show (album),
  host (artist), release date, genre *Podcast*, and the artwork.
- **Follow** a show (button above the episode grid) to keep it archived:
  *Check for new* queues every episode published since you followed it that
  isn't already downloaded. Set *Settings → Podcasts · auto-check* to do that
  every 1–24 hours (the queue starts by itself when something new turns up).
- **OPML** — import your subscriptions from another podcast app, or export
  the shows you follow.
- **Local feeds** — every archived show gets an RSS feed at
  `http://127.0.0.1:8765/feeds/<Show>.xml` (listed under Library → Podcasts).
  Subscribe from a podcast app on this computer to play your archive. To use
  it from a phone, run Chive on your LAN (`CHIVE_HOST=0.0.0.0` plus
  `CHIVE_ALLOWED_HOSTS=<this computer's name or IP>`) — see *Security*.

### Notifications

Turn on *Settings → Desktop notifications* to hear about finished and failed
downloads, a drained queue, and newly found podcast episodes while Chive is in
a background tab. The choice is stored in this browser only.

> **Beyond YouTube.** This started as a YouTube channel archiver and is becoming
> a broader archival tool — YouTube and podcasts today, more sources to come.
> Source-specific features (SponsorBlock, Shorts) stay scoped to YouTube; the
> bridge drives `yt-dlp`, which supports many sources.

## How it works

### Scraping
`POST /api/scrape` calls
`yt-dlp --flat-playlist --dump-json --playlist-items <start>:<end> <url>`
and parses one JSON entry per line. Only metadata is fetched at this
stage — no video bytes leave YouTube's servers until you queue
something.

### Downloading
Each queued item becomes a `yt-dlp` subprocess with:

- `-f <format>` mapped from the quality preset
- `--sponsorblock-remove <categories>` (cut) or `--sponsorblock-mark`
  (label as chapters) when categories are selected
- `--write-subs`/`--embed-subs` (with `--sub-langs`) when subtitles are on —
  embedding keeps captions aligned when segments are cut
- `--merge-output-format mp4` for video presets, `--extract-audio` for
  audio presets
- `-o '<dir>/%(uploader)s/%(title)s [%(id)s].%(ext)s'`

When the `yt_dlp` module is importable, downloads run through
`chive/ytdlp_runner.py`, which drives yt-dlp via its Python API and emits
progress from a `progress_hooks` callback with an explicit `flush()`. This is
the only thing that streams progress reliably on Windows, where yt-dlp buffers
its own stdout and the bar would otherwise jump straight to 100%. It also
reports one figure for the *whole item* — a "best video + best audio" pick is
two downloads, which would otherwise run the bar 0→100% twice. (Without the
module, Chive falls back to the `yt-dlp` binary + `--progress-template`.)
Progress, speed, ETA and byte counts are broadcast to the UI over Server-Sent
Events at `/api/events`.

Every item keeps a readable log (`<state dir>/item-logs/<id>.log`): the
command, yt-dlp's status lines, a progress summary every 10%, and the outcome.
It survives restarts; retries and resumes append below a separator.

Quitting the server stops running downloads; they come back as **pending**
and resume from their `.part` file on the next start.

The server runs under **waitress** when available, which serves concurrent
requests reliably while the SSE stream is open (the Flask dev server can drop
a request mid-download); it falls back to the dev server otherwise. A live
connection pill, disk-space guard, and aggregate queue stats round out the
monitoring.

### Security
The bridge drives yt-dlp, writes files and can run `pip` on your machine, so
it only obeys its own page:

- **Host check** — requests must be addressed to a loopback name
  (`127.0.0.1`, `localhost`, `::1`). This blocks *DNS rebinding*, where a
  malicious site re-points its domain at your machine to read the API. If you
  reach Chive by another name, add it to `CHIVE_ALLOWED_HOSTS`.
- **JSON-only writes** — state-changing requests must be
  `application/json` and must not carry a foreign `Origin`. Browsers can't
  send that cross-site without a CORS preflight, which Chive never grants, so
  other websites can't queue downloads or change settings behind your back.

Binding to a non-loopback `CHIVE_HOST` (e.g. `0.0.0.0` for a NAS) accepts any
hostname unless `CHIVE_ALLOWED_HOSTS` is set — and there is no login, so only
do that on a network you trust.

### SponsorBlock categories
| Key              | Meaning                       |
| ---------------- | ----------------------------- |
| `sponsor`        | paid promotion                |
| `selfpromo`      | the creator's own merch / Patreon |
| `interaction`    | "like and subscribe" prompts  |
| `intro`          | bumper / channel intro        |
| `outro`          | end-card music                |
| `preview`        | recap / "coming up" segments  |
| `filler`         | tangents, off-topic asides    |
| `music_offtopic` | music in non-music videos     |

## Layout

```
.
├── server.py               # entry point: python server.py
├── chive/                  # the bridge
│   ├── config.py           #   environment, paths, constants
│   ├── settings.py         #   persisted preferences (validated)
│   ├── models.py           #   the queue item
│   ├── manager.py          #   queue, workers, logs, events
│   ├── commands.py         #   yt-dlp command building
│   ├── progress.py         #   yt-dlp output → progress & phases
│   ├── ytdlp.py            #   locating / invoking yt-dlp & ffmpeg
│   ├── ytdlp_runner.py     #   flushing API wrapper (run as a subprocess)
│   ├── youtube.py          #   scraping, search, filter & sort
│   ├── podcasts.py         #   podcast search & RSS feeds
│   ├── music.py            #   album/track lookup & metadata
│   ├── web.py              #   any-site lookup
│   ├── tagging.py          #   tags, cover art, chapter-based album splitting
│   ├── library.py          #   file naming & already-downloaded detection
│   ├── catalog.py          #   the Library: index, listing, safe access, delete
│   ├── feeds.py            #   local podcast RSS feeds & OPML
│   ├── subscriptions.py    #   followed podcasts & auto-sync
│   ├── api.py              #   Flask app, request guard, SSE
│   └── util.py             #   formatting, disk, process helpers
├── static/
│   ├── index.html          # the page
│   ├── style.css           # Dusk / Dawn themes
│   └── js/                 # ES modules (no build step)
│       ├── app.js          #   state, rendering, bindings
│       ├── queue-model.js  #   pure queue/event logic
│       ├── format.js       #   pure formatting
│       ├── library.js      #   the Library view & player
│       ├── notifications.js#   desktop notifications, tab-title progress
│       └── …               #   api, dom, modal, log viewer, easter eggs
│   └── fonts/              # self-hosted fonts (OFL)
└── tests/                  # Python, JS and browser tests
```

## Development

```sh
pip install -r requirements.txt ruff
ruff check .                                  # lint
python -m unittest discover -s tests          # Python: unit + integration
npm test                                      # JS: pure modules (Node ≥ 20)
npm install && npx playwright install chromium
npm run test:e2e                              # browser end-to-end smoke test
```

Everything runs offline: yt-dlp is mocked or scripted for unit tests, and the
integration and end-to-end tests download from a local HTTP server. CI runs
all of it on Linux and Windows.

## Notes

- The bridge binds to `127.0.0.1` by default. Don't expose it on a
  public interface — see *Security*.
- Queue, settings, and recent channels persist to
  `~/.chive/state.json` (override with `CHIVE_STATE_DIR`; a pre-existing
  `~/.ytarchive` is honored for backward compatibility).
- Downloads default to `~/Downloads/Chive` — outside the app folder, so
  updating or deleting the app can't take your library with it. An existing,
  non-empty `downloads/` folder from an earlier version keeps being used.

## License

[MIT](LICENSE). Contributions welcome.
