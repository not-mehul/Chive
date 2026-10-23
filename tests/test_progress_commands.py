"""Output-line parsing, whole-item progress aggregation, and command building."""

import unittest
from unittest import mock

import support
from support import make_item

from chive import commands, progress, ytdlp, ytdlp_runner
from chive.settings import Settings


class ParseLineTests(unittest.TestCase):
    def test_progress_line(self):
        it = make_item()
        kind = progress.apply_line(it, "[YTPROG] 5242880|10485760|10485760|1258291.2|4|downloading|mp4")
        self.assertEqual(kind, progress.PROGRESS)
        self.assertEqual(it.progress, 50.0)
        self.assertEqual(it.total_bytes, 10485760.0)
        self.assertAlmostEqual(it.speed_bps, 1258291.2)
        self.assertEqual(it.speed, "1.2 MiB/s")
        self.assertEqual(it.eta, "00:04")
        self.assertEqual(it.message, "Downloading video")

    def test_missing_fields(self):
        it = make_item()
        progress.apply_line(it, "[YTPROG] 1000|NA|NA|NA|NA|downloading|NA")
        self.assertEqual(it.downloaded_bytes, 1000.0)
        self.assertEqual(it.progress, 0.0)
        self.assertIsNone(it.speed)
        self.assertIsNone(it.eta)

    def test_estimate_used_when_total_missing(self):
        it = make_item()
        progress.apply_line(it, "[YTPROG] 50|None|200|None|None|downloading|m4a")
        self.assertEqual(it.progress, 25.0)
        self.assertEqual(it.message, "Downloading audio")

    def test_finished_pins_100(self):
        it = make_item()
        progress.apply_line(it, "[YTPROG] 100|100|100|0|0|finished|mp4")
        self.assertEqual(it.progress, 100.0)

    def test_truncated_progress_line_is_ignored(self):
        it = make_item()
        progress.apply_line(it, "[YTPROG] 1|2")
        self.assertEqual(it.progress, 0.0)

    def test_postprocess_phases(self):
        it = make_item()
        self.assertEqual(progress.apply_line(it, "[YTPP] Merger"), progress.POSTPROCESS)
        self.assertEqual(it.message, "Merging streams")
        for name, phase in (("ModifyChapters", "Cutting segments"),
                            ("EmbedSubtitle", "Embedding subtitles"),
                            ("FFmpegMetadata", "Writing metadata"),
                            ("SomethingNew", "Processing")):
            self.assertEqual(progress.pp_phase(name), phase)

    def test_outcomes(self):
        it = make_item()
        self.assertEqual(progress.apply_line(
            it, "[download] v: v has already been recorded in the archive"), progress.ARCHIVED)
        self.assertEqual(progress.apply_line(
            it, "[download] /x/a.mp4 has already been downloaded"), progress.ON_DISK)

    def test_destination_keeps_the_bar(self):
        # With whole-item progress, a new stream's Destination must not reset it.
        it = make_item(progress=55.0, speed="1 MiB/s")
        progress.apply_line(it, "[download] Destination: /x/a.f140.m4a")
        self.assertEqual(it.progress, 55.0)
        self.assertEqual(it.message, "Downloading audio")
        self.assertIsNone(it.speed)

    def test_filepath_and_errors(self):
        it = make_item()
        progress.apply_line(it, "filepath:/x/Video [abc].mp4\r")
        self.assertEqual(it.output_file, "/x/Video [abc].mp4")
        progress.apply_line(it, "ERROR: [youtube] v: Video unavailable")
        self.assertEqual(it.message, "[youtube] v: Video unavailable")
        progress.apply_line(it, "ERROR: Sign in to confirm your age")
        self.assertEqual(it.message, progress.AGE_RESTRICTED)

    def test_status_markers(self):
        it = make_item()
        progress.apply_line(it, "[SponsorBlock] Fetching segments")
        self.assertEqual(it.message, "Querying SponsorBlock")

    def test_summary(self):
        it = make_item(progress=42.0, total_bytes=1024.0 ** 2, speed="1.0 MiB/s", eta="00:01")
        self.assertEqual(progress.summary(it), "[progress]  42.0% of 1.0 MiB at 1.0 MiB/s eta 00:01")


class AggregateProgressTests(unittest.TestCase):
    """ytdlp_runner.progress_fields folds a video+audio pick into one figure."""

    def _hook(self, fid, done, total, status="downloading", ext="mp4", speed=100.0):
        return {"info_dict": {"format_id": fid, "ext": ext}, "downloaded_bytes": done,
                "total_bytes": total, "total_bytes_estimate": None, "speed": speed,
                "eta": 1, "status": status}

    def test_two_streams_report_one_overall_progress(self):
        plan = ytdlp_runner.new_plan()
        plan["formats"] = [("137", 900), ("140", 100)]
        done, total, _, _, eta, status, _ = ytdlp_runner.progress_fields(self._hook("137", 450, 900), plan)
        self.assertEqual((done, total), (450, 1000))          # 45%, not 50%
        self.assertAlmostEqual(eta, 5.5)
        # The video stream finishing is not the item finishing...
        _, _, _, _, _, status, _ = ytdlp_runner.progress_fields(
            self._hook("137", 900, 900, "finished"), plan)
        self.assertEqual(status, "downloading")
        # ...and the audio stream continues from 90% instead of restarting at 0%.
        done, total, *_ = ytdlp_runner.progress_fields(self._hook("140", 50, 100, ext="m4a"), plan)
        self.assertEqual((done, total), (950, 1000))
        *_, status, _ = ytdlp_runner.progress_fields(self._hook("140", 100, 100, "finished", "m4a"), plan)
        self.assertEqual(status, "finished")

    def test_actual_size_replaces_estimate(self):
        plan = ytdlp_runner.new_plan()
        plan["formats"] = [("137", 900), ("140", 100)]
        ytdlp_runner.progress_fields(self._hook("137", 1000, 1000, "finished"), plan)
        done, total, *_ = ytdlp_runner.progress_fields(self._hook("140", 0, 100, ext="m4a"), plan)
        self.assertEqual((done, total), (1000, 1100))

    def test_single_stream_and_unknown_sizes_pass_through(self):
        plan = ytdlp_runner.new_plan()
        hook = self._hook("18", 10, 100)
        self.assertEqual(ytdlp_runner.progress_fields(hook, plan)[:2], (10, 100))
        plan["formats"] = [("137", None), ("140", None)]
        self.assertEqual(ytdlp_runner.progress_fields(self._hook("140", 10, None), plan)[:2], (10, None))

    def test_subtitle_downloads_are_skipped(self):
        self.assertIsNone(ytdlp_runner.progress_fields(self._hook("en", 1, 2, ext="vtt"),
                                                        ytdlp_runner.new_plan()))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        self.s.update({"download_dir": str(support.tmpdir())})

    def test_sponsorblock_modes(self):
        cmd = commands.build_command(make_item(sponsorblock=["sponsor"]), self.s)
        self.assertIn("--sponsorblock-remove", cmd)
        cmd = commands.build_command(make_item(sponsorblock=["sponsor"], sponsorblock_mode="mark"), self.s)
        self.assertIn("--sponsorblock-mark", cmd)
        self.assertIn("--embed-chapters", cmd)

    def test_print_never_silences_status_lines(self):
        for it in (make_item(), make_item(source="podcast", collection="Show")):
            cmd = commands.build_command(it, self.s)
            self.assertIn("--print", cmd)
            self.assertIn("--no-quiet", cmd)
            self.assertEqual(cmd[-1], it.url)

    def test_subtitles_video_only(self):
        self.s.update({"subtitles": True, "subtitle_auto": True})
        cmd = commands.build_command(make_item(quality="1080p"), self.s)
        self.assertIn("--embed-subs", cmd)
        self.assertIn("--write-auto-subs", cmd)
        self.assertNotIn("--write-subs", commands.build_command(make_item(quality="mp3"), self.s))

    def test_audio_preset(self):
        cmd = commands.build_command(make_item(quality="mp3"), self.s)
        self.assertEqual(cmd[cmd.index("--audio-format") + 1], "mp3")
        self.assertNotIn("--merge-output-format", cmd)
        self.assertNotIn("--embed-chapters", cmd)

    def test_archive_and_cookies(self):
        self.s.update({"use_archive": True, "cookies_file": "/c.txt"})
        cmd = commands.build_command(make_item(), self.s)
        self.assertTrue(cmd[cmd.index("--download-archive") + 1].endswith("archive.txt"))
        self.assertEqual(cmd[cmd.index("--cookies") + 1], "/c.txt")
        self.s.update({"cookies_browser": "firefox"})       # a browser wins over a file
        cmd = commands.build_command(make_item(), self.s)
        self.assertIn("--cookies-from-browser", cmd)
        self.assertNotIn("--cookies", cmd)

    def test_unknown_quality_falls_back(self):
        cmd = commands.build_command(make_item(quality="bogus"), self.s)
        self.assertIn("-f", cmd)

    def test_podcast_command(self):
        it = make_item(video_id="g1", url="http://cdn/ep1.mp3", title="Ep 1: Hello",
                       source="podcast", collection="Test Pod")
        cmd = commands.build_command(it, self.s)
        self.assertNotIn("-f", cmd)
        self.assertNotIn("--sponsorblock-remove", cmd)
        out = cmd[cmd.index("-o") + 1]
        self.assertIn("Test Pod", out)
        self.assertIn("Ep 1 Hello [", out)                    # ':' sanitised, tag appended

    def test_same_title_episodes_get_distinct_files(self):
        out = lambda c: c[c.index("-o") + 1]
        a = commands.build_command(make_item(video_id="a", title="Trailer", source="podcast"), self.s)
        b = commands.build_command(make_item(video_id="b", title="Trailer", source="podcast"), self.s)
        self.assertNotEqual(out(a), out(b))

    def test_binary_fallback_uses_progress_template(self):
        with mock.patch.object(ytdlp, "runner_cmd", return_value=None), \
                mock.patch.object(ytdlp, "base_cmd", return_value=["yt-dlp"]):
            cmd = commands.build_command(make_item(), self.s)
        self.assertEqual(cmd[0], "yt-dlp")
        self.assertIn("[YTPROG]", cmd[cmd.index("--progress-template") + 1])

    def test_runner_used_when_available(self):
        with mock.patch.object(ytdlp, "runner_cmd", return_value=["py", "runner.py"]):
            cmd = commands.build_command(make_item(), self.s)
        self.assertEqual(cmd[:2], ["py", "runner.py"])
        self.assertNotIn("--progress-template", cmd)


if __name__ == "__main__":
    unittest.main()
