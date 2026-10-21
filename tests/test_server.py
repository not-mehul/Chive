"""Unit tests for the Chive bridge — helpers, API, and offline runner integration.

Run with:  python3 -m unittest discover -s tests
No third-party test deps; everything runs offline (yt-dlp is mocked, or fed
from a local HTTP server).
"""

import json
import os
import tempfile
import unittest
from unittest import mock

# Point persistence at a throwaway dir before importing the module under test —
# importing it constructs the QueueManager singleton, which reads state on init.
os.environ["CHIVE_STATE_DIR"] = tempfile.mkdtemp(prefix="chive-test-")

import server  # noqa: E402


class IsShortTests(unittest.TestCase):
    def test_shorts_url_is_short(self):
        self.assertTrue(server._is_short("https://www.youtube.com/shorts/abc123"))

    def test_regular_watch_url_is_not_short(self):
        self.assertFalse(server._is_short("https://www.youtube.com/watch?v=abc123"))

    def test_short_regular_upload_is_not_hidden(self):
        # Regression: a sub-minute regular upload must NOT be classified as a
        # Short just because of its duration (it has a /watch URL, not /shorts/).
        self.assertFalse(server._is_short("https://www.youtube.com/watch?v=clip"))

    def test_none_url(self):
        self.assertFalse(server._is_short(None))


class ScrapePaginationTests(unittest.TestCase):
    def setUp(self):
        server._SCRAPE_CACHE.clear()
        self.entries = [
            {
                "id": f"v{i}",
                "url": (
                    f"https://www.youtube.com/shorts/v{i}"
                    if i % 10 == 0
                    else f"https://www.youtube.com/watch?v=v{i}"
                ),
                "title": f"Title {i}",
                "channel": "Test Channel",
            }
            for i in range(1, 121)
        ]

    def _fake_run(self, *args, **kwargs):
        r = mock.Mock()
        r.returncode = 0
        r.stdout = "\n".join(json.dumps(e) for e in self.entries)
        r.stderr = ""
        return r

    def test_page_window_is_sliced(self):
        with mock.patch.object(server.shutil, "which", return_value="yt-dlp"), \
                mock.patch.object(server.subprocess, "run", side_effect=self._fake_run):
            page1 = server.scrape_channel("http://chan", 1, 50, ignore_shorts=False)
            page2 = server.scrape_channel("http://chan", 2, 50, ignore_shorts=False)
        self.assertEqual(page1["videos"][0]["video_id"], "v1")
        self.assertEqual(page2["videos"][0]["video_id"], "v51")
        self.assertEqual(page1["page_entries"], 50)

    def test_shorts_filtered_but_counted_for_pagination(self):
        with mock.patch.object(server.shutil, "which", return_value="yt-dlp"), \
                mock.patch.object(server.subprocess, "run", side_effect=self._fake_run):
            page = server.scrape_channel("http://chan", 1, 50, ignore_shorts=True)
        # v10, v20, v30, v40, v50 are /shorts/ → 5 hidden, 45 shown.
        self.assertEqual(page["skipped_shorts"], 5)
        self.assertEqual(page["count"], 45)
        # page_entries reflects the raw window so pagination doesn't stall.
        self.assertEqual(page["page_entries"], 50)

    def test_cache_avoids_refetch(self):
        with mock.patch.object(server.shutil, "which", return_value="yt-dlp"), \
                mock.patch.object(server.subprocess, "run", side_effect=self._fake_run) as run:
            server.scrape_channel("http://chan", 1, 50, ignore_shorts=False)
            server.scrape_channel("http://chan", 1, 50, ignore_shorts=False)
        # Second call for the same/shallower window is served from cache.
        self.assertEqual(run.call_count, 1)

    def test_title_filter_spans_whole_channel(self):
        with mock.patch.object(server.shutil, "which", return_value="yt-dlp"), \
                mock.patch.object(server.subprocess, "run", side_effect=self._fake_run):
            page = server.scrape_channel(
                "http://chan", 1, 50, ignore_shorts=False,
                filters={"query": "Title 11"})
        self.assertTrue(page["filtered"])
        # "Title 11" and "Title 110"..."Title 119" match across the channel.
        titles = [v["title"] for v in page["videos"]]
        self.assertIn("Title 11", titles)
        self.assertTrue(all("Title 11" in t for t in titles))

    def test_sort_oldest_reverses(self):
        with mock.patch.object(server.shutil, "which", return_value="yt-dlp"), \
                mock.patch.object(server.subprocess, "run", side_effect=self._fake_run):
            page = server.scrape_channel(
                "http://chan", 1, 50, ignore_shorts=True, sort="oldest")
        # Oldest-first → the highest-numbered non-short comes first.
        self.assertEqual(page["videos"][0]["video_id"], "v119")


class BuildCommandTests(unittest.TestCase):
    def _item(self, **kw):
        base = dict(id="x", video_id="v", url="https://www.youtube.com/watch?v=v",
                    title="t", thumbnail=None, duration=None, quality="1080p",
                    sponsorblock=["sponsor"])
        base.update(kw)
        return server.QueueItem(**base)

    def test_sponsorblock_remove_is_default(self):
        cmd = server.manager._build_command(self._item())
        self.assertIn("--sponsorblock-remove", cmd)
        self.assertNotIn("--sponsorblock-mark", cmd)

    def test_sponsorblock_mark_mode(self):
        cmd = server.manager._build_command(self._item(sponsorblock_mode="mark"))
        self.assertIn("--sponsorblock-mark", cmd)
        self.assertNotIn("--sponsorblock-remove", cmd)
        self.assertIn("--embed-chapters", cmd)  # marking needs chapters

    def test_subtitles_embedded_when_enabled(self):
        server.manager._subtitles = True
        server.manager._subtitle_embed = True
        try:
            cmd = server.manager._build_command(self._item(quality="1080p"))
        finally:
            server.manager._subtitles = False
        self.assertIn("--write-subs", cmd)
        self.assertIn("--embed-subs", cmd)

    def test_no_subtitles_for_audio_preset(self):
        server.manager._subtitles = True
        try:
            cmd = server.manager._build_command(self._item(quality="mp3"))
        finally:
            server.manager._subtitles = False
        self.assertNotIn("--write-subs", cmd)


class ValidateSourceTests(unittest.TestCase):
    def test_handle(self):
        self.assertEqual(server.validate_source("@veritasium"),
                         ("url", "https://www.youtube.com/@veritasium"))

    def test_bare_host_gets_scheme(self):
        kind, val = server.validate_source("youtube.com/@x")
        self.assertEqual(kind, "url")
        self.assertTrue(val.startswith("https://"))

    def test_plain_text_is_query(self):
        self.assertEqual(server.validate_source("veritasium science"), ("query", "veritasium science"))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            server.validate_source("   ")


class DownloadedIdsTests(unittest.TestCase):
    def test_detects_disk_and_archive_ignores_part(self):
        import pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "Uploader").mkdir()
        (d / "Uploader" / "A Video [abcDEF12345].mp4").write_text("x")
        (d / "Uploader" / "Half [zzzZZZ99999].mp4.part").write_text("x")
        (d / "archive.txt").write_text("youtube arch1234567\n")
        prev = server.manager._download_dir
        server.manager._download_dir = d
        server.manager._dl_scan = None
        try:
            ids = server.manager.downloaded_ids()
        finally:
            server.manager._download_dir = prev
            server.manager._dl_scan = None
        self.assertIn("abcDEF12345", ids)       # on disk
        self.assertIn("arch1234567", ids)        # in archive.txt
        self.assertNotIn("zzzZZZ99999", ids)     # .part is in-progress, ignored


_RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>Test Pod</title>
<image><url>http://art/show.jpg</url></image>
<item><title>Ep 1: Hello</title><guid>g1</guid>
<pubDate>Tue, 10 Jun 2025 10:00:00 GMT</pubDate>
<itunes:duration>1:02:03</itunes:duration>
<enclosure url="http://cdn/ep1.mp3" type="audio/mpeg" length="123"/></item>
<item><title>Ep 2: World</title><guid>g2</guid><itunes:duration>1830</itunes:duration>
<enclosure url="http://cdn/ep2.m4a" type="audio/x-m4a" length="456"/></item>
</channel></rss>"""

_ITUNES = json.dumps({"results": [
    {"collectionName": "Test Pod", "artistName": "Auth", "feedUrl": "http://feed",
     "artworkUrl600": "http://art.jpg", "trackCount": 42, "primaryGenreName": "Tech"},
    {"collectionName": "No Feed"},  # dropped — no feedUrl
]}).encode()


class PodcastTests(unittest.TestCase):
    def test_search_podcasts(self):
        with mock.patch.object(server, "_http_get", return_value=_ITUNES):
            shows = server.search_podcasts("x")
        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0]["name"], "Test Pod")
        self.assertEqual(shows[0]["feed_url"], "http://feed")

    def test_fetch_episodes(self):
        with mock.patch.object(server, "_http_get", return_value=_RSS):
            r = server.fetch_episodes("http://feed", page=1)
        self.assertEqual(r["podcast"], "Test Pod")
        self.assertEqual(r["total"], 2)
        eps = {v["title"]: v for v in r["videos"]}
        self.assertEqual(eps["Ep 1: Hello"]["duration"], 3723)   # 1:02:03
        self.assertEqual(eps["Ep 2: World"]["duration"], 1830)
        self.assertEqual(eps["Ep 1: Hello"]["url"], "http://cdn/ep1.mp3")
        self.assertEqual(eps["Ep 1: Hello"]["collection"], "Test Pod")

    def test_fetch_episodes_title_filter(self):
        with mock.patch.object(server, "_http_get", return_value=_RSS):
            r = server.fetch_episodes("http://feed", page=1, query="world")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["videos"][0]["title"], "Ep 2: World")

    def test_safe_segment(self):
        self.assertEqual(server._safe_segment("A/B: 100%"), "AB 100")
        self.assertEqual(server._safe_segment(""), "untitled")

    def test_podcast_command_has_no_format(self):
        it = server.QueueItem(id="x", video_id="g1", url="http://cdn/ep1.mp3",
                              title="Ep 1: Hello", thumbnail=None, duration=10,
                              quality="1080p", sponsorblock=[], source="podcast",
                              collection="Test Pod")
        cmd = server.manager._build_podcast_command(it)
        self.assertNotIn("-f", cmd)
        self.assertNotIn("--sponsorblock-remove", cmd)
        self.assertEqual(cmd[-1], "http://cdn/ep1.mp3")
        out = cmd[cmd.index("-o") + 1]
        self.assertIn("Test Pod", out)
        self.assertIn("Ep 1 Hello", out)  # ':' sanitised out


class ByteHelperTests(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(server._human_bytes(1536), "1.5 KiB")
        self.assertEqual(server._human_bytes(0), "0 B")

    def test_fmt_eta(self):
        self.assertEqual(server._fmt_eta(7), "00:07")
        self.assertEqual(server._fmt_eta(3661), "1:01:01")
        self.assertIsNone(server._fmt_eta(None))


class ProgressTemplateTests(unittest.TestCase):
    def _item(self, quality="1080p"):
        return server.QueueItem(id="x", video_id="v", url="u", title="t",
                                thumbnail=None, duration=None, quality=quality,
                                sponsorblock=[])

    def test_template_line_drives_progress(self):
        it = self._item()
        server.manager._parse_progress(
            it, "[YTPROG] 5242880|10485760|10485760|1258291.2|4|downloading|mp4")
        self.assertEqual(it.progress, 50.0)
        self.assertEqual(it.total_bytes, 10485760.0)
        self.assertEqual(it.downloaded_bytes, 5242880.0)
        self.assertAlmostEqual(it.speed_bps, 1258291.2)
        self.assertEqual(it.eta, "00:04")
        self.assertEqual(it.message, "Downloading video")

    def test_template_na_fields(self):
        it = self._item()
        server.manager._parse_progress(
            it, "[YTPROG] 1000|NA|NA|NA|NA|downloading|NA")
        # No total → percent unchanged, but downloaded bytes still tracked.
        self.assertEqual(it.downloaded_bytes, 1000.0)
        self.assertIsNone(it.speed)
        self.assertIsNone(it.eta)

    def test_template_finished_pins_100(self):
        it = self._item()
        server.manager._parse_progress(
            it, "[YTPROG] 100|100|100|0|0|finished|mp4")
        self.assertEqual(it.progress, 100.0)

    def test_build_command_streams_progress(self):
        # Either the API runner (flushing hook) is used, or the binary fallback
        # carries the --progress-template; both yield [YTPROG] lines.
        cmd = server.manager._build_command(self._item())
        if any("ytdlp_runner.py" in part for part in cmd):
            self.assertNotIn("--progress-template", cmd)  # the hook replaces it
        else:
            self.assertIn("--progress-template", cmd)
            self.assertIn("[YTPROG]", cmd[cmd.index("--progress-template") + 1])

    def test_pp_phase_mapping(self):
        self.assertEqual(server.manager._pp_phase("Merger"), "Merging streams")
        self.assertEqual(server.manager._pp_phase("ModifyChapters"), "Cutting segments")
        self.assertEqual(server.manager._pp_phase("EmbedSubtitle"), "Embedding subtitles")

    def test_ytpp_line_sets_phase(self):
        it = self._item()
        server.manager._parse_progress(it, "[YTPP] Merger")
        self.assertEqual(it.message, "Merging streams")


class BroadcastEvictionTests(unittest.TestCase):
    def test_full_listener_keeps_newest_event(self):
        m = server.manager
        q = m.listen()
        try:
            # Saturate the listener so the next put would overflow.
            for i in range(q.maxsize):
                q.put_nowait(f"old{i}")
            m._broadcast({"type": "update", "marker": "terminal"})
            drained = []
            while not q.empty():
                drained.append(q.get_nowait())
            # The newest (terminal) event must have survived the overflow...
            self.assertTrue(any("terminal" in x for x in drained))
            # ...and the queue must never have exceeded its bound.
            self.assertLessEqual(len(drained), q.maxsize)
        finally:
            m.drop(q)


def _item(**kw):
    base = dict(id="x", video_id="v", url="https://www.youtube.com/watch?v=v",
                title="t", thumbnail=None, duration=None, quality="1080p",
                sponsorblock=[])
    base.update(kw)
    return server.QueueItem(**base)


class QuietModeTests(unittest.TestCase):
    def test_print_does_not_silence_status_lines(self):
        # --print implies --quiet, which hid the archive-skip message and left
        # the log empty; --no-quiet must accompany it.
        for it in (_item(), _item(source="podcast", collection="Show")):
            cmd = server.manager._build_command(it)
            self.assertIn("--print", cmd)
            self.assertIn("--no-quiet", cmd)


class PodcastFilenameTests(unittest.TestCase):
    def test_same_title_episodes_get_distinct_files(self):
        out = lambda c: c[c.index("-o") + 1]  # noqa: E731
        a = server.manager._build_command(_item(id="a", video_id="guid-a", title="Trailer",
                                                source="podcast", collection="Show"))
        b = server.manager._build_command(_item(id="b", video_id="guid-b", title="Trailer",
                                                source="podcast", collection="Show"))
        self.assertNotEqual(out(a), out(b))

    def test_downloaded_detection_by_tag_and_legacy_title(self):
        import pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "Show").mkdir()
        tag = server._pod_tag("guid-a")
        (d / "Show" / f"Trailer [{tag}].mp3").write_text("x")
        (d / "Show" / "Old Episode.mp3").write_text("x")          # pre-tag naming
        (d / "Show" / f"Partial [{server._pod_tag('guid-c')}].mp3.part").write_text("x")
        prev = server.manager._download_dir
        server.manager._download_dir = d
        try:
            have = server.manager.podcast_downloaded("Show", [
                {"video_id": "guid-a", "title": "Trailer"},
                {"video_id": "guid-b", "title": "Trailer"},        # same title, not on disk
                {"video_id": "guid-c", "title": "Partial"},        # only a .part
                {"video_id": "guid-d", "title": "Old Episode"},
            ])
        finally:
            server.manager._download_dir = prev
        self.assertEqual(have, {"guid-a", "guid-d"})


class ItemLogTests(unittest.TestCase):
    def test_log_survives_losing_memory(self):
        m = server.manager
        m._open_item_log("logtest-1")
        m._log_line("logtest-1", "hello")
        m._log_line("logtest-1", "world")
        m._close_item_log("logtest-1")
        with m._lock:
            m._logs.pop("logtest-1", None)      # as after a server restart
        self.assertEqual(m.get_log("logtest-1"), ["hello", "world"])
        m._delete_log_file("logtest-1")
        self.assertIsNone(m.get_log("logtest-1"))

    def test_unsafe_id_never_becomes_a_path(self):
        self.assertIsNone(server.QueueManager._log_path("../../etc/passwd"))
        self.assertIsNone(server.QueueManager._log_path(""))


class ShutdownTests(unittest.TestCase):
    def test_shutdown_stops_running_downloads(self):
        import subprocess
        import sys
        m = server.QueueManager()   # a private manager — shutdown is one-way
        kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if server.IS_WINDOWS
              else {"start_new_session": True})
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        try:
            with m._lock:
                m._active_procs["sleeper"] = proc
            m.shutdown(timeout=5)
            self.assertIsNotNone(proc.poll())
            self.assertFalse(m.is_running())
        finally:
            if proc.poll() is None:
                proc.kill()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_malformed_page_is_not_a_500(self):
        with mock.patch.object(server, "scrape_channel",
                               return_value={"videos": [], "page": 1}) as sc:
            r = self.client.post("/api/scrape", json={"url": "@chan", "page": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sc.call_args[0][1], 1)       # fell back to page 1

    def test_feed_url_must_be_http(self):
        r = self.client.post("/api/podcast/episodes", json={"feed_url": "file:///etc/hostname"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("http", r.get_json()["error"])
        with self.assertRaises(ValueError):
            server._http_get("file:///etc/hostname")

    def test_api_errors_are_json(self):
        r = self.client.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_event_stream_has_no_hop_by_hop_header(self):
        # waitress rejects a "Connection" header from a WSGI app (PEP 3333),
        # which killed the SSE stream before it opened.
        r = self.client.get("/api/events")
        try:
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("Connection", r.headers)
        finally:
            r.close()

    def test_add_broadcasts_one_event_per_batch(self):
        m = server.manager
        q = m.listen()
        try:
            with mock.patch.object(m, "_persist"):
                added = m.add([{"video_id": f"batch{i:07d}"} for i in range(5)], "1080p", [])
            events = []
            while not q.empty():
                events.append(json.loads(q.get_nowait()))
            queued = [e for e in events if e["type"] == "queued"]
            self.assertEqual(len(queued), 1)
            self.assertEqual(len(queued[0]["items"]), 5)
        finally:
            m.drop(q)
            for it in added:
                m.remove(it.id)


@unittest.skipUnless(server._ytdlp_runner_cmd(), "yt_dlp module not installed")
class RunnerIntegrationTests(unittest.TestCase):
    """Real downloads through ytdlp_runner.py, offline, from a local HTTP server."""

    @classmethod
    def setUpClass(cls):
        import functools
        import http.server
        import threading
        cls.media = tempfile.mkdtemp()
        with open(os.path.join(cls.media, "ep.mp3"), "wb") as fh:
            fh.write(os.urandom(256 * 1024))
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass
        handler = functools.partial(Quiet, directory=cls.media)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/ep.mp3"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        import pathlib
        self.m = server.manager
        self.prev = (self.m._download_dir, self.m._embed_metadata)
        self.m._download_dir = pathlib.Path(tempfile.mkdtemp())
        self.m._embed_metadata = False   # no ffmpeg needed for random bytes

    def tearDown(self):
        self.m._download_dir, self.m._embed_metadata = self.prev

    def _run(self, item, extra=()):
        real = self.m._build_command
        with mock.patch.object(self.m, "_build_command",
                               side_effect=lambda it: real(it)[:-1] + list(extra) + [it.url]):
            self.m._process(item)
        return item

    def test_download_completes_with_readable_log(self):
        it = self._run(_item(id="int-1", video_id="g-1", url=self.url, title="Ep",
                             source="podcast", collection="Show"))
        self.assertEqual(it.status, "completed", it.message)
        self.assertEqual(it.message, "Saved")
        self.assertTrue(it.output_file and os.path.exists(it.output_file))
        log = self.m.get_log("int-1")
        self.assertTrue(any(ln.startswith("=== ") for ln in log))
        self.assertTrue(any("[download] Destination" in ln for ln in log))  # not silenced
        self.assertTrue(any(ln.startswith("[progress]") for ln in log))
        self.assertFalse(any("[YTPROG]" in ln for ln in log))              # summarised
        self.m._delete_log_file("int-1")

    def test_archive_skip_is_reported(self):
        archive = os.path.join(str(self.m._download_dir), "archive.txt")
        extra = ["--download-archive", archive]
        first = self._run(_item(id="int-2", video_id="g-2", url=self.url, title="A",
                                source="podcast", collection="Show"), extra)
        self.assertEqual(first.message, "Saved")
        again = self._run(_item(id="int-3", video_id="g-3", url=self.url, title="B",
                                source="podcast", collection="Show"), extra)
        self.assertEqual(again.status, "completed")
        self.assertEqual(again.message, "Skipped — already in archive")
        for i in ("int-2", "int-3"):
            self.m._delete_log_file(i)


if __name__ == "__main__":
    unittest.main()
