"""Tagging, album splitting (with real ffmpeg), the runner's finishing step,
music & any-site resolving, and the music/podcast/web command builders."""

import json
import unittest
from pathlib import Path
from unittest import mock

import support
from support import HAS_FFMPEG, make_audio, make_image, make_item, probe

from chive import commands, music, podcasts, tagging, web, ytdlp, ytdlp_runner
from chive.settings import Settings


class HeuristicTests(unittest.TestCase):
    def test_strip_noise(self):
        self.assertEqual(tagging.strip_noise("Discovery (Full Album)"), "Discovery")
        self.assertEqual(tagging.strip_noise("Song [Official Audio] (HD)"), "Song")
        self.assertEqual(tagging.strip_noise("Album - Full Album"), "Album")
        self.assertEqual(tagging.strip_noise("Remastered (2011)"), "Remastered (2011)")   # kept

    def test_clean_artist(self):
        self.assertEqual(tagging.clean_artist("Daft Punk - Topic"), "Daft Punk")
        self.assertEqual(tagging.clean_artist("ArtistVEVO"), "Artist")
        self.assertEqual(tagging.clean_artist(None), "")

    def test_guess_artist_album(self):
        g = tagging.guess_artist_album
        self.assertEqual(g("Daft Punk - Discovery (Full Album)", "Some Channel"), ("Daft Punk", "Discovery"))
        self.assertEqual(g("Discovery", "Daft Punk - Topic"), ("Daft Punk", "Discovery"))
        self.assertEqual(g("anything", "x", artist="A", album="B (Full Album)"), ("A", "B"))
        self.assertEqual(g(None, None), ("Unknown Artist", "Unknown Album"))

    def test_clean_track_title(self):
        c = tagging.clean_track_title
        self.assertEqual(c("01. Intro"), "Intro")
        self.assertEqual(c("3 - Song"), "Song")
        self.assertEqual(c("[04] Song"), "Song")
        self.assertEqual(c("Artist - Song (Official Audio)", "Artist"), "Song")
        self.assertEqual(c("1979"), "1979")                         # a title that is a number
        self.assertEqual(c("99 Luftballons"), "99 Luftballons")      # leading number, no separator

    def test_track_filename_is_safe(self):
        self.assertEqual(tagging.track_filename(3, "A/B: C?", ".mp3"), "03 - AB C.mp3")


class FfmpegArgTests(unittest.TestCase):
    def test_metadata_args(self):
        args = tagging.metadata_args({"title": "T", "artist": "A", "track": 2, "track_total": 9, "genre": ""})
        self.assertEqual(args, ["-metadata", "title=T", "-metadata", "artist=A", "-metadata", "track=2/9"])

    def test_mp3_with_cover_and_slice(self):
        cmd = tagging.tag_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), {"title": "T"},
                                  Path("cover.jpg"), start=10, duration=5)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "10.000")
        self.assertEqual(cmd[cmd.index("-t") + 1], "5.000")
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))           # fast seek before input
        self.assertIn("attached_pic", cmd)
        self.assertIn("-id3v2_version", cmd)
        self.assertEqual(cmd[cmd.index("-map_chapters") + 1], "-1")   # tracks drop album chapters
        self.assertEqual(cmd[-1], "out.mp3")

    def test_video_files_keep_all_streams_and_no_cover(self):
        cmd = tagging.tag_command("ffmpeg", Path("in.mp4"), Path("out.mp4"), {}, Path("c.jpg"))
        self.assertEqual(cmd[cmd.index("-map") + 1], "0")
        self.assertNotIn("attached_pic", cmd)
        self.assertNotIn("-map_chapters", cmd)                        # a single file keeps chapters

    def test_opus_gets_no_cover(self):
        cmd = tagging.tag_command("ffmpeg", Path("in.opus"), Path("out.opus"), {}, Path("c.jpg"))
        self.assertNotIn("attached_pic", cmd)

    def test_cover_command(self):
        self.assertIn("crop", " ".join(tagging.cover_command("ffmpeg", Path("a"), Path("b.jpg"), square=True)))
        self.assertNotIn("crop", " ".join(tagging.cover_command("ffmpeg", Path("a"), Path("b.jpg"), square=False)))

    def test_chapter_spans(self):
        spans = tagging.chapter_spans([
            {"start_time": 20, "title": "C"},                     # no end: runs to the total
            {"start_time": 0, "end_time": 10, "title": "A"},
            {"start_time": 10, "title": "B"},                     # no end: runs to the next
            {"start_time": 5, "end_time": 5, "title": "zero"},    # zero length: dropped
            {"title": "no start"},
        ], total=30)
        self.assertEqual([(s, d, t) for s, d, t in spans], [(0, 10, "A"), (10, 10, "B"), (20, 10, "C")])
        self.assertEqual(tagging.chapter_spans([{"start_time": 0}], total=None), [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RealFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.dir = support.tmpdir()

    def test_square_cover_from_a_widescreen_image(self):
        cover = tagging.prepare_cover("ffmpeg", str(make_image(self.dir / "t.png")), self.dir / "cover.jpg", True)
        self.assertEqual(cover, self.dir / "cover.jpg")
        self.assertEqual(probe(cover)["streams"][0][1], "mjpeg")
        self.assertIsNone(tagging.prepare_cover("ffmpeg", None, self.dir / "none.jpg", True))
        self.assertIsNone(tagging.prepare_cover("ffmpeg", "file:///etc/passwd", self.dir / "x.jpg", True))

    def test_split_album_into_tagged_tracks(self):
        for ext in ("mp3", "m4a"):
            src = make_audio(self.dir / f"album.{ext}", 30, ext)
            cover = tagging.prepare_cover("ffmpeg", str(make_image(self.dir / "t.png")), self.dir / "c.jpg", True)
            files = tagging.split_album(
                "ffmpeg", src, [(0, 10, "01. Opening"), (10, 10, "Middle"), (20, 10, "Closing")],
                {"artist": "Artist", "album": "Album", "date": "2024"}, cover, self.dir / ext)
            self.assertEqual([Path(f["path"]).name for f in files],
                             [f"01 - Opening.{ext}", f"02 - Middle.{ext}", f"03 - Closing.{ext}"])
            for n, f in enumerate(files, 1):
                p = probe(f["path"])
                self.assertAlmostEqual(p["duration"], 10, delta=0.6)
                self.assertEqual(p["tags"]["track"], f"{n}/3")
                self.assertEqual(p["tags"]["album"], "Album")
                self.assertEqual(p["tags"]["artist"], "Artist")
                self.assertIn(("video", "mjpeg", 1), p["streams"])       # embedded cover

    def test_tag_file_in_place(self):
        src = make_audio(self.dir / "ep.mp3", 3)
        tagging.tag_file("ffmpeg", src, {"title": "Ep: 1", "album": "Show", "genre": "Podcast"}, None)
        tags = probe(src)["tags"]
        self.assertEqual((tags["title"], tags["album"], tags["genre"]), ("Ep: 1", "Show", "Podcast"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["ep.mp3"])   # no temp left


class RunnerFinishTests(unittest.TestCase):
    def test_pop_chive_tags(self):
        argv, tags = ytdlp_runner.pop_chive_tags(["-f", "ba", "--chive-tags", '{"mode":"album"}', "URL"])
        self.assertEqual((argv, tags), (["-f", "ba", "URL"], {"mode": "album"}))
        self.assertEqual(ytdlp_runner.pop_chive_tags(["URL"]), (["URL"], None))
        self.assertEqual(ytdlp_runner.pop_chive_tags(["--chive-tags", "{bad", "U"]), (["U"], None))

    def test_without_tags_or_ffmpeg_reports_the_file_as_is(self):
        info = {"filepath": "/nope.mp3", "title": "T", "duration": 3}
        self.assertEqual(ytdlp_runner.finish(info, None, "ffmpeg")[0]["path"], "/nope.mp3")
        f = support.tmpdir() / "a.mp3"
        f.write_bytes(b"x")
        with mock.patch.object(ytdlp_runner, "_emit") as emit:
            files = ytdlp_runner.finish({"filepath": str(f)}, {"mode": "single"}, None)
        self.assertEqual(files[0]["path"], str(f))
        self.assertIn("ffmpeg not found", emit.call_args[0][0])

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_album_split_end_to_end(self):
        d = support.tmpdir()
        src = make_audio(d / "Album.mp3", 30)
        thumb = make_image(d / "thumb.png")
        info = {"filepath": str(src), "title": "Artist - Album", "duration": 30, "thumbnail": str(thumb),
                "chapters": [{"start_time": 0, "end_time": 15, "title": "A"},
                             {"start_time": 15, "end_time": 30, "title": "B"}]}
        tags = {"mode": "album", "artist": "Artist", "album": "Album", "square": True,
                "cover_save": str(d / "cover.jpg")}
        with mock.patch.object(ytdlp_runner, "_emit"):
            files = ytdlp_runner.finish(info, tags, "ffmpeg")
        self.assertEqual([f["title"] for f in files], ["A", "B"])
        self.assertFalse(src.exists())                           # the unsplit file is removed…
        self.assertTrue((d / "cover.jpg").exists())              # …the folder cover is kept
        self.assertEqual(probe(files[1]["path"])["tags"]["track"], "2/2")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_album_without_chapters_becomes_one_tagged_track_and_keep_full(self):
        d = support.tmpdir()
        src = make_audio(d / "Song.mp3", 3)
        with mock.patch.object(ytdlp_runner, "_emit") as emit:
            files = ytdlp_runner.finish({"filepath": str(src), "title": "Song"},
                                        {"mode": "album", "artist": "A", "album": "B"}, "ffmpeg")
        self.assertEqual(files[0]["path"], str(src))
        self.assertTrue(any("no chapters" in c[0][0] for c in emit.call_args_list))
        self.assertEqual(probe(src)["tags"]["title"], "Song")
        self.assertEqual([p.name for p in d.iterdir()], ["Song.mp3"])     # temp cover cleaned up

        src = make_audio(d / "Full.mp3", 20)
        with mock.patch.object(ytdlp_runner, "_emit"):
            ytdlp_runner.finish({"filepath": str(src), "chapters": [
                {"start_time": 0, "end_time": 10, "title": "x"}, {"start_time": 10, "end_time": 20, "title": "y"}]},
                {"mode": "album", "keep_full": True}, "ffmpeg")
        self.assertTrue(src.exists())


def _entries(*items):
    return list(items)


class MusicResolveTests(unittest.TestCase):
    def run_with(self, entries, text):
        with mock.patch.object(ytdlp, "run_json_lines", return_value=entries) as run:
            return music.resolve(text), run

    def test_search_assumes_full_albums(self):
        r, run = self.run_with([{"id": "a", "title": "Daft Punk - Discovery (Full Album)", "channel": "X"}],
                               "daft punk discovery")
        self.assertIn("ytsearch20:daft punk discovery full album", run.call_args[0][0])
        v = r["videos"][0]
        self.assertEqual((r["kind"], v["music_mode"], v["artist"], v["album"]),
                         ("search", "album", "Daft Punk", "Discovery"))

    def test_single_video_with_chapters_is_an_album(self):
        r, _ = self.run_with([{"id": "a", "title": "Album", "uploader": "Band - Topic", "upload_date": "20190101",
                               "chapters": [{}, {}, {}], "webpage_url": "https://y/a"}], "https://y/a")
        v = r["videos"][0]
        self.assertEqual((r["kind"], v["music_mode"], v["chapters"], v["artist"], v["year"]),
                         ("single", "album", 3, "Band", "2019"))

    def test_single_video_without_chapters_is_a_track(self):
        r, _ = self.run_with([{"id": "a", "title": "Song"}], "https://y/a")
        self.assertEqual(r["videos"][0]["music_mode"], "track")

    def test_playlist_is_tracks_with_positions(self):
        entries = [{"id": f"t{i}", "_type": "url", "url": f"https://y/t{i}", "title": f"0{i}. Artist - Song {i}",
                    "playlist_title": "Album - Allium", "playlist_uploader": "Artist - Topic",
                    "playlist_index": i, "playlist_count": 2} for i in (1, 2)]
        r, _ = self.run_with(entries, "https://y/list")
        self.assertEqual((r["kind"], r["album"], r["artist"]), ("playlist", "Allium", "Artist"))
        self.assertEqual([(v["track"], v["title"], v["track_total"]) for v in r["videos"]],
                         [(1, "Song 1", 2), (2, "Song 2", 2)])

    def test_errors(self):
        with self.assertRaises(ValueError):
            music.resolve("  ")
        with mock.patch.object(ytdlp, "run_json_lines", return_value=[]), self.assertRaises(RuntimeError):
            music.resolve("https://y/empty")

    def test_item_extra_validates(self):
        e = music.item_extra({"music_mode": "bogus", "title": "T", "artist": 5, "track": "x"})
        self.assertEqual(e["music_mode"], "album")
        self.assertEqual(e["tags"]["album"], "T")
        self.assertEqual(e["tags"]["artist"], "5")
        e = music.item_extra({"music_mode": "track", "title": "T", "album": "A", "track": "3", "track_total": -1})
        self.assertEqual((e["tags"]["track"], e["tags"].get("track_total")), (3, None))


class WebResolveTests(unittest.TestCase):
    def test_single(self):
        with mock.patch.object(ytdlp, "run_json_lines", return_value=[
                {"id": 42, "title": "Talk", "extractor_key": "Vimeo", "webpage_url": "https://vimeo.com/42"}]):
            r = web.resolve("vimeo.com/42")
        self.assertEqual((r["kind"], r["site"]), ("single", "Vimeo"))
        self.assertEqual(r["videos"][0]["video_id"], "42")
        self.assertEqual(r["videos"][0]["url"], "https://vimeo.com/42")

    def test_playlist(self):
        with mock.patch.object(ytdlp, "run_json_lines", return_value=[
                {"id": "a", "_type": "url", "url": "https://x/a", "playlist_title": "P", "ie_key": "Site"},
                {"id": "b", "_type": "url", "url": "https://x/b"}]):
            r = web.resolve("https://x/list")
        self.assertEqual((r["kind"], r["title"], len(r["videos"])), ("playlist", "P", 2))

    def test_bad_urls(self):
        for bad in ("", "not a url", "ftp://x.com/a"):
            with self.assertRaises(ValueError):
                web.require_url(bad)


class SourceCommandTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        self.s.update({"download_dir": str(support.tmpdir())})
        p = mock.patch.object(ytdlp, "runner_cmd", return_value=["py", "runner.py"])
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def tags_of(cmd):
        return json.loads(cmd[cmd.index("--chive-tags") + 1])

    def test_music_album(self):
        it = make_item(source="music", quality="m4a", thumbnail="https://t/x.webp",
                       extra=music.item_extra({"music_mode": "album", "artist": "A/C", "album": "B"}))
        cmd = commands.build_command(it, self.s)
        out = cmd[cmd.index("-o") + 1]
        self.assertTrue(out.endswith(str(Path("Music") / "AC" / "B" / "B.%(ext)s")))
        self.assertEqual(cmd[cmd.index("--audio-format") + 1], "m4a")
        tags = self.tags_of(cmd)
        self.assertEqual((tags["mode"], tags["square"], tags["cover_url"]), ("album", True, "https://t/x.webp"))
        self.assertTrue(tags["cover_save"].endswith("cover.jpg"))
        self.assertNotIn("--embed-metadata", cmd)                 # the tagger does it properly

    def test_music_track_name(self):
        it = make_item(source="music", quality="mp3", extra=music.item_extra(
            {"music_mode": "track", "title": "Song", "artist": "A", "album": "B", "track": 4}))
        out = commands.build_command(it, self.s)
        self.assertTrue(out[out.index("-o") + 1].endswith("04 - Song.%(ext)s"))

    def test_music_binary_fallback_uses_ytdlp_splitting(self):
        it = make_item(source="music", quality="mp3", extra=music.item_extra({"music_mode": "album"}))
        with mock.patch.object(ytdlp, "runner_cmd", return_value=None):
            cmd = commands.build_command(it, self.s)
        self.assertIn("--split-chapters", cmd)
        self.assertNotIn("--chive-tags", cmd)

    def test_podcast_tags_and_show_cover(self):
        ep = {"title": "Ep", "collection": "Show", "author": "Host", "upload_date": "20250102",
              "artwork": "https://a/show.jpg"}
        it = make_item(source="podcast", video_id="g", thumbnail="https://a/show.jpg", title="Ep",
                       collection="Show", extra=podcasts.item_extra(ep))
        tags = self.tags_of(commands.build_command(it, self.s))
        self.assertEqual((tags["album"], tags["artist"], tags["date"], tags["genre"]),
                         ("Show", "Host", "2025-01-02", "Podcast"))
        self.assertTrue(tags["cover_save"].endswith(str(Path("Show") / "cover.jpg")))
        it.thumbnail = "https://a/episode.jpg"                     # episode art: not the folder cover
        self.assertIsNone(self.tags_of(commands.build_command(it, self.s))["cover_save"])

    def test_web_goes_under_web_without_sponsorblock(self):
        it = make_item(source="web", sponsorblock=["sponsor"], url="https://vimeo.com/1")
        cmd = commands.build_command(it, self.s)
        self.assertIn(str(Path("Web") / "%(extractor_key)s"), cmd[cmd.index("-o") + 1])
        self.assertNotIn("--sponsorblock-remove", cmd)


if __name__ == "__main__":
    unittest.main()
