"""Real downloads through chive/ytdlp_runner.py and real yt-dlp — offline, from
a local HTTP server. Skipped when the yt_dlp module isn't installed."""

import unittest
from unittest import mock

import support
from support import MediaServer, make_manager, status_of, wait_for

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
        (it,) = self.m.add([{"video_id": vid, "url": url or self.url, "title": title,
                             "collection": "Show"}], None, [], source="podcast")
        real = self.m.build_command
        with mock.patch.object(self.m, "build_command",
                               side_effect=lambda i: real(i)[:-1] + list(extra) + [i.url]):
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


if __name__ == "__main__":
    unittest.main()
