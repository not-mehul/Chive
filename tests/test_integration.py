"""Real downloads through chive/ytdlp_runner.py and real yt-dlp — offline, from
a local HTTP server. Skipped when the yt_dlp module isn't installed."""

import json
import subprocess
import unittest
from unittest import mock

import support
from support import HAS_FFMPEG, MediaServer, make_audio, make_image, make_manager, probe, status_of, wait_for

from chive import ytdlp


@unittest.skipUnless(ytdlp.runner_cmd(), "yt_dlp module not installed")
class RunnerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = MediaServer()
        cls.url = cls.media.add("ep.mp3", 256 * 1024)

    @classmethod
    def tearDownClass(cls):
        cls.media.close()

    def setUp(self):
        # Random bytes aren't a real MP3, so skip ffmpeg metadata embedding.
        self.m = make_manager(start_dispatcher=True, embed_metadata=False)
        self.addCleanup(self.m.shutdown)

    def _download(self, vid, title, url=None, extra=()):
        real = self.m.build_command
        # Patch *before* queueing: once the queue is running, the dispatcher
        # may start the new item immediately.
        with mock.patch.object(self.m, "build_command",
                               side_effect=lambda i: real(i)[:-1] + list(extra) + [i.url]):
            (it,) = self.m.add([{"video_id": vid, "url": url or self.url, "title": title,
                                 "collection": "Show"}], None, [], source="podcast")
            self.m.start()
            wait_for(lambda: status_of(self.m, it.id) in ("completed", "failed"), timeout=60)
        return self.m.item(it.id)

    def test_download_with_readable_log(self):
        it = self._download("g-1", "Ep")
        self.assertEqual((it["status"], it["message"]), ("completed", "Saved"), it["message"])
        self.assertTrue(it["output_file"].endswith(".mp3"))
        log = self.m.get_log(it["id"])
        self.assertTrue(any("[download] Destination" in ln for ln in log))   # not silenced
        self.assertTrue(any(ln.startswith("[progress]") for ln in log))
        self.assertFalse(any("[YTPROG]" in ln for ln in log))
        self.assertFalse(any("ChivePlan" in ln for ln in log))               # internal step hidden

    def test_same_titled_episodes_are_both_saved(self):
        a = self._download("guid-a", "Trailer")
        b = self._download("guid-b", "Trailer", url=self.media.add("ep2.mp3", 64 * 1024))
        self.assertEqual((a["message"], b["message"]), ("Saved", "Saved"))
        self.assertNotEqual(a["output_file"], b["output_file"])
        files = sorted(p.name for p in (self.m.download_dir / "Show").iterdir())
        self.assertEqual(len(files), 2)
        have = self.m.podcast_downloaded("Show", [{"video_id": "guid-a", "title": "Trailer"},
                                                  {"video_id": "guid-c", "title": "Trailer"}])
        self.assertEqual(have, {"guid-a"})

    def test_archive_skip_is_reported(self):
        archive = str(self.m.download_dir / "archive.txt")
        extra = ["--download-archive", archive]
        first = self._download("g-2", "A", extra=extra)
        self.assertEqual(first["message"], "Saved")
        again = self._download("g-3", "B", extra=extra)
        self.assertEqual((again["status"], again["message"]),
                         ("completed", "Skipped — already in archive"))

    def test_shutdown_then_resume_from_partial_file(self):
        self.media.delay = 0.05
        self.addCleanup(setattr, self.media, "delay", 0.0)
        url = self.media.add("slow.mp3", 1024 * 1024)
        (it,) = self.m.add([{"video_id": "slow", "url": url, "title": "Slow",
                             "collection": "Show"}], None, [], source="podcast")
        self.m.start()
        wait_for(lambda: (self.m.item(it.id) or {}).get("progress", 0) > 10, timeout=30)
        self.m.shutdown()
        restarted = support.QueueManager(state_file=self.m.state_file,
                                         item_log_dir=self.m.item_log_dir)
        self.addCleanup(restarted.shutdown)
        self.media.delay = 0.0
        restarted.start()
        wait_for(lambda: status_of(restarted, it.id) == "completed", timeout=60)
        log = restarted.get_log(it.id)
        self.assertTrue(any("Resuming download at byte" in ln for ln in log), "\n".join(log))
        self.assertTrue(any("resuming: Slow" in ln for ln in log))


@unittest.skipUnless(ytdlp.runner_cmd() and HAS_FFMPEG, "needs the yt_dlp module and ffmpeg")
class MusicIntegrationTests(unittest.TestCase):
    """Real music downloads through yt-dlp + the runner's tagger/splitter."""

    @classmethod
    def setUpClass(cls):
        cls.media = MediaServer()
        d = support.tmpdir()
        cls.album_url = cls.media.add("album.mp3", 1)
        cls.media.files["album.mp3"] = make_audio(d / "album.mp3", 30).read_bytes()
        cls.cover_url = cls.media.add("thumb.png", 1)
        cls.media.files["thumb.png"] = make_image(d / "thumb.png").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media.close()

    def test_full_album_is_split_into_tagged_tracks(self):
        """yt-dlp only learns chapters from a real site, so feed it an info JSON
        (as a site extractor would produce) via --load-info-json."""
        d = support.tmpdir()
        info = d / "album.info.json"
        info.write_text(json.dumps({
            "id": "albumvid001", "title": "Band - Allium (Full Album)", "uploader": "Band - Topic",
            "url": self.album_url, "ext": "mp3", "extractor": "generic", "extractor_key": "Generic",
            "webpage_url": self.album_url, "duration": 30, "thumbnail": self.cover_url,
            "chapters": [{"start_time": 0, "end_time": 10, "title": "01. Shoots"},
                         {"start_time": 10, "end_time": 20, "title": "Band - Bulbs (Official Audio)"},
                         {"start_time": 20, "end_time": 30, "title": "Blossom"}]}))
        folder = d / "Music" / "Band" / "Allium"
        tags = {"mode": "album", "artist": "Band", "album": "Allium", "album_artist": "Band", "date": "2024",
                "square": True, "cover_save": str(folder / "cover.jpg")}
        out = subprocess.run(
            [*ytdlp.runner_cmd(), "--load-info-json", str(info), "--newline", "--no-colors",
             "-o", str(folder / "Allium.%(ext)s"), "--no-quiet", "--chive-tags", json.dumps(tags)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        reported = [json.loads(ln[8:]) for ln in out.stdout.splitlines() if ln.startswith("[YTFILE]")]
        self.assertEqual([f["title"] for f in reported], ["Shoots", "Bulbs", "Blossom"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["01 - Shoots.mp3", "02 - Bulbs.mp3", "03 - Blossom.mp3", "cover.jpg"])
        p = probe(folder / "02 - Bulbs.mp3")
        self.assertEqual((p["tags"]["title"], p["tags"]["track"], p["tags"]["album"], p["tags"]["artist"],
                          p["tags"]["date"]), ("Bulbs", "2/3", "Allium", "Band", "2024"))
        self.assertIn(("video", "mjpeg", 1), p["streams"])

    def test_queued_track_is_tagged_and_lands_in_the_library(self):
        m = make_manager(start_dispatcher=True)
        self.addCleanup(m.shutdown)
        (it,) = m.add([{"video_id": "t1", "url": self.album_url, "title": "Green Shoots",
                        "thumbnail": self.cover_url, "artist": "The Chives", "album": "Allium",
                        "year": "2024", "track": 3, "track_total": 9, "music_mode": "track"}],
                      None, [], source="music")
        m.start()
        wait_for(lambda: status_of(m, it.id) in ("completed", "failed"), timeout=60)
        item = m.item(it.id)
        self.assertEqual(item["status"], "completed", item["message"])
        path = m.download_dir / "Music" / "The Chives" / "Allium" / "03 - Green Shoots.mp3"
        self.assertEqual(item["output_file"], str(path))
        tags = probe(path)["tags"]
        self.assertEqual((tags["title"], tags["track"], tags["album"]), ("Green Shoots", "3/9", "Allium"))
        lib = m.library.get("Music/The Chives/Allium/03 - Green Shoots.mp3")
        self.assertEqual((lib["source"], lib["video_id"], lib["artist"]), ("music", "t1", "The Chives"))
        log = m.get_log(it.id)
        self.assertIn("[postprocess] ChiveFinish", log)


if __name__ == "__main__":
    unittest.main()
