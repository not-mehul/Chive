"""YouTube scraping/search, podcast search/feeds, and the on-disk library."""

import json
import subprocess
import unittest
from unittest import mock

import support

from chive import library, podcasts, youtube, ytdlp


def _entries(n=120):
    return [{"id": f"v{i}",
             "url": (f"https://www.youtube.com/shorts/v{i}" if i % 10 == 0
                     else f"https://www.youtube.com/watch?v=v{i}"),
             "title": f"Title {i}", "channel": "Test Channel",
             "duration": i * 10, "view_count": 1000 - i,
             "upload_date": f"2024{(i % 12) + 1:02d}01"}
            for i in range(1, n + 1)]


class ScrapeTests(unittest.TestCase):
    def setUp(self):
        youtube.SCRAPE_CACHE.clear()
        self.entries = _entries()
        p1 = mock.patch.object(ytdlp, "available", return_value=True)
        p2 = mock.patch.object(ytdlp, "run_json_lines", side_effect=lambda *a, **k: self.entries)
        p1.start()
        self.run = p2.start()
        self.addCleanup(mock.patch.stopall)

    def test_page_window_is_sliced(self):
        p1 = youtube.scrape_channel("http://chan", 1, 50, ignore_shorts=False)
        p2 = youtube.scrape_channel("http://chan", 2, 50, ignore_shorts=False)
        self.assertEqual(p1["videos"][0]["video_id"], "v1")
        self.assertEqual(p2["videos"][0]["video_id"], "v51")
        self.assertEqual(p1["channel"], "Test Channel")

    def test_shorts_hidden_but_counted_for_paging(self):
        page = youtube.scrape_channel("http://chan", 1, 50, ignore_shorts=True)
        self.assertEqual(page["skipped_shorts"], 5)
        self.assertEqual(page["count"], 45)
        self.assertEqual(page["page_entries"], 50)

    def test_cache_serves_shallower_pages(self):
        youtube.scrape_channel("http://chan", 2, 50)
        youtube.scrape_channel("http://chan", 1, 50)
        self.assertEqual(self.run.call_count, 1)
        youtube.scrape_channel("http://other", 1, 50)
        self.assertEqual(self.run.call_count, 2)

    def test_title_filter_spans_whole_channel(self):
        page = youtube.scrape_channel("http://chan", 1, 50, ignore_shorts=False,
                                      filters={"query": "title 11"})
        self.assertTrue(page["filtered"])
        self.assertEqual(page["total"], 11)          # 11, 110..119
        self.assertTrue(all("Title 11" in v["title"] for v in page["videos"]))

    def test_range_filters(self):
        page = youtube.scrape_channel("http://chan", 1, 50, ignore_shorts=False,
                                      filters={"duration_min": 100, "duration_max": 200})
        self.assertEqual([v["duration"] for v in page["videos"]],
                         list(range(100, 201, 10)))
        page = youtube.scrape_channel("http://chan", 1, 50, ignore_shorts=False,
                                      filters={"date_from": "20241101", "date_to": "20241231"})
        self.assertTrue(all(v["upload_date"] >= "20241101" for v in page["videos"]))

    def test_sorts(self):
        first = lambda sort: youtube.scrape_channel(
            "http://chan", 1, 50, ignore_shorts=True, sort=sort)["videos"][0]["video_id"]
        self.assertEqual(first("oldest"), "v119")
        self.assertEqual(first("longest"), "v119")
        self.assertEqual(first("shortest"), "v1")
        self.assertEqual(first("most_viewed"), "v1")
        self.assertEqual(first("least_viewed"), "v119")

    def test_page_clamped_when_filter_shrinks_results(self):
        page = youtube.scrape_channel("http://chan", 9, 50, filters={"query": "title 11"})
        self.assertEqual(page["page"], 1)

    def test_missing_metadata_passes_filters(self):
        self.assertTrue(youtube.passes_filters({"title": "x"}, {"views_min": 10, "duration_max": 5}))

    def test_requires_ytdlp(self):
        with mock.patch.object(ytdlp, "available", return_value=False):
            with self.assertRaises(RuntimeError):
                youtube.scrape_channel("http://chan", 1, 50)


class RunJsonLinesTests(unittest.TestCase):
    def _proc(self, rc, out="", err=""):
        return mock.Mock(returncode=rc, stdout=out, stderr=err)

    def test_parses_objects_and_skips_noise(self):
        out = '{"id": 1}\nnot json\n\n[1]\n{"id": 2}\n'
        with mock.patch.object(ytdlp, "base_cmd", return_value=["yt-dlp"]), \
                mock.patch.object(ytdlp.subprocess, "run", return_value=self._proc(0, out)):
            self.assertEqual(ytdlp.run_json_lines([], 5, "scrape"), [{"id": 1}, {"id": 2}])

    def test_failure_surfaces_last_error_line(self):
        with mock.patch.object(ytdlp, "base_cmd", return_value=["yt-dlp"]), \
                mock.patch.object(ytdlp.subprocess, "run",
                                  return_value=self._proc(1, "", "WARNING: x\nERROR: no such channel")):
            with self.assertRaisesRegex(RuntimeError, "no such channel"):
                ytdlp.run_json_lines([], 5, "scrape")

    def test_timeout(self):
        with mock.patch.object(ytdlp, "base_cmd", return_value=["yt-dlp"]), \
                mock.patch.object(ytdlp.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("yt-dlp", 5)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                ytdlp.run_json_lines([], 5, "scrape")


class ChannelSearchAndValidationTests(unittest.TestCase):
    def test_search_dedupes_by_channel(self):
        hits = [{"channel": "A", "channel_url": "http://a", "channel_id": "1"},
                {"channel": "A", "channel_url": "http://a", "channel_id": "1"},
                {"uploader": "B", "uploader_url": "http://b"},
                {"channel": "No URL"}]
        with mock.patch.object(ytdlp, "run_json_lines", return_value=hits):
            found = youtube.search_channels("x")
        self.assertEqual([c["name"] for c in found], ["A", "B"])

    def test_validate_source(self):
        self.assertEqual(youtube.validate_source("@veritasium"),
                         ("url", "https://www.youtube.com/@veritasium"))
        self.assertEqual(youtube.validate_source("youtube.com/@x"), ("url", "https://youtube.com/@x"))
        self.assertEqual(youtube.validate_source("veritasium science"), ("query", "veritasium science"))
        for bad in ("   ", "ftp://x.com/a"):
            with self.assertRaises(ValueError):
                youtube.validate_source(bad)

    def test_is_short(self):
        self.assertTrue(youtube.is_short("https://www.youtube.com/shorts/abc"))
        self.assertFalse(youtube.is_short("https://www.youtube.com/watch?v=clip"))
        self.assertFalse(youtube.is_short(None))

    def test_entry_to_video(self):
        v = youtube.entry_to_video({"id": "abc", "thumbnails": [{"url": "s"}, {"url": "big"}]})
        self.assertEqual(v["thumbnail"], "big")
        self.assertEqual(youtube.entry_to_video({"id": "abc"})["thumbnail"],
                         "https://i.ytimg.com/vi/abc/hqdefault.jpg")
        self.assertIsNone(youtube.entry_to_video({}))

    def test_norm_date(self):
        self.assertEqual(youtube.norm_date("2024-03-05"), "20240305")
        self.assertIsNone(youtube.norm_date("2024"))


_RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>Test Pod</title>
<image><url>http://art/show.jpg</url></image>
<item><title>Ep 1: Hello</title><guid>g1</guid>
<pubDate>Tue, 10 Jun 2025 10:00:00 GMT</pubDate>
<itunes:duration>1:02:03</itunes:duration>
<enclosure url="http://cdn/ep1.mp3" type="audio/mpeg" length="123"/></item>
<item><title>Ep 2: World</title><guid>g2</guid><itunes:duration>1830</itunes:duration>
<itunes:image href="http://art/ep2.jpg"/>
<enclosure url="http://cdn/ep2.m4a" type="audio/x-m4a" length="456"/></item>
<item><title>No audio</title><guid>g3</guid></item>
</channel></rss>"""

_ITUNES = json.dumps({"results": [
    {"collectionName": "Test Pod", "artistName": "Auth", "feedUrl": "http://feed",
     "artworkUrl600": "http://art.jpg", "trackCount": 42, "primaryGenreName": "Tech"},
    {"collectionName": "No Feed"},
]}).encode()


class PodcastTests(unittest.TestCase):
    def setUp(self):
        podcasts.FEED_CACHE.clear()

    def test_search(self):
        with mock.patch.object(podcasts, "http_get", return_value=_ITUNES):
            shows = podcasts.search_podcasts("x")
        self.assertEqual([s["name"] for s in shows], ["Test Pod"])
        self.assertEqual(shows[0]["episode_count"], 42)

    def test_parse_feed(self):
        feed = podcasts.parse_feed(_RSS)
        self.assertEqual(feed["podcast"], "Test Pod")
        self.assertEqual(feed["artwork"], "http://art/show.jpg")
        eps = {e["title"]: e for e in feed["episodes"]}
        self.assertEqual(set(eps), {"Ep 1: Hello", "Ep 2: World"})        # no enclosure → dropped
        self.assertEqual(eps["Ep 1: Hello"]["duration"], 3723)
        self.assertEqual(eps["Ep 1: Hello"]["upload_date"], "20250610")
        self.assertEqual(eps["Ep 1: Hello"]["thumbnail"], "http://art/show.jpg")
        self.assertEqual(eps["Ep 2: World"]["thumbnail"], "http://art/ep2.jpg")

    def test_bad_feeds(self):
        with self.assertRaisesRegex(RuntimeError, "parse"):
            podcasts.parse_feed(b"<not xml")
        with self.assertRaisesRegex(RuntimeError, "podcast RSS"):
            podcasts.parse_feed(b"<html/>")

    def test_paging_filter_and_cache(self):
        with mock.patch.object(podcasts, "http_get", return_value=_RSS) as get:
            r = podcasts.fetch_episodes("http://feed", page=1, page_size=1)
            self.assertEqual((r["total"], r["total_pages"], r["count"]), (2, 2, 1))
            r = podcasts.fetch_episodes("http://feed", page=1, query="world")
            self.assertEqual([v["title"] for v in r["videos"]], ["Ep 2: World"])
        self.assertEqual(get.call_count, 1)       # the feed was fetched once

    def test_results_are_copies(self):
        with mock.patch.object(podcasts, "http_get", return_value=_RSS):
            r = podcasts.fetch_episodes("http://feed", page=1)
            r["videos"][0]["downloaded"] = True
            again = podcasts.fetch_episodes("http://feed", page=1)
        self.assertNotIn("downloaded", again["videos"][0])

    def test_only_http_urls(self):
        for bad in ("file:///etc/hostname", "ftp://x/feed", "http://", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                podcasts.http_get(bad)
        with self.assertRaisesRegex(RuntimeError, "could not fetch"):
            podcasts.fetch_episodes("file:///etc/hostname", page=1)

    def test_durations(self):
        self.assertEqual(podcasts.parse_duration("3600"), 3600)
        self.assertEqual(podcasts.parse_duration("02:03"), 123)
        self.assertIsNone(podcasts.parse_duration("soon"))
        self.assertIsNone(podcasts.parse_duration(None))
        self.assertIsNone(podcasts.rss_date("not a date"))


class LibraryTests(unittest.TestCase):
    def test_safe_segment(self):
        self.assertEqual(library.safe_segment("A/B: 100%"), "AB 100")
        self.assertEqual(library.safe_segment("  ..dots.. "), "dots")
        self.assertEqual(library.safe_segment(""), "untitled")
        self.assertEqual(len(library.safe_segment("x" * 500)), 120)

    def test_video_ids_and_archive(self):
        d = support.tmpdir()
        (d / "Uploader").mkdir()
        (d / "Uploader" / "A Video [abcDEF12345].mp4").write_text("x")
        (d / "Uploader" / "Half [zzzZZZ99999].mp4.part").write_text("x")
        (d / "archive.txt").write_text("youtube arch1234567\n\n")
        self.assertEqual(library.scan_video_ids(d), {"abcDEF12345"})
        self.assertEqual(library.archive_ids(d), {"arch1234567"})
        self.assertEqual(library.scan_video_ids(d / "missing"), set())

    def test_podcast_detection_by_tag_and_legacy_title(self):
        d = support.tmpdir()
        (d / library.podcast_filename("Trailer", "guid-a", "mp3")).write_text("x")
        (d / "Old Episode.mp3").write_text("x")
        (d / (library.podcast_filename("Partial", "guid-c", "mp3") + ".part")).write_text("x")
        have = library.podcast_downloaded(d, [
            {"video_id": "guid-a", "title": "Trailer"},
            {"video_id": "guid-b", "title": "Trailer"},      # same title, not on disk
            {"video_id": "guid-c", "title": "Partial"},      # only a .part
            {"video_id": "guid-d", "title": "Old Episode"},  # pre-tag file name
        ])
        self.assertEqual(have, {"guid-a", "guid-d"})


if __name__ == "__main__":
    unittest.main()
