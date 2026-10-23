"""The library (index, listing, safe file access, delete), local podcast
feeds, OPML, followed-show syncing, and their API endpoints."""

import json
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import support
from support import fake_command, make_manager, status_of, wait_for

from chive import catalog, feeds, podcasts, subscriptions
from chive.api import create_app


def _populate(root: Path) -> None:
    files = {
        "Uploader/A Video [abcDEF12345].mp4": 3000,
        "Garden Talk/Ep One [245be930].mp3": 2000,
        "Garden Talk/cover.jpg": 10,
        "Music/Band/Album/01 - Intro.mp3": 1000,
        "Music/Band/Album/02 - Song.mp3": 1500,
        "Music/Band/Album/cover.jpg": 10,
        "Web/Vimeo/Someone/Talk [42].webm": 500,
        "loose.mp3": 100,
        "notes.txt": 5,
        ".hidden.mp3": 5,
    }
    for rel, size in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        catalog.invalidate()
        self.root = support.tmpdir()
        _populate(self.root)
        self.index = catalog.LibraryIndex(support.tmpdir() / "library.json")

    def test_listing_merges_scan_and_index(self):
        self.index.record(self.root, [{"path": str(self.root / "Music/Band/Album/02 - Song.mp3"),
                                       "title": "Song", "track": 2}],
                          {"source": "music", "collection": "Album", "artist": "Band", "video_id": "vid"})
        lib = catalog.list_library(self.root, self.index)
        by_path = {i["path"]: i for i in lib["items"]}
        self.assertEqual(lib["total"], 6)                                  # media only, no hidden/txt
        song = by_path["Music/Band/Album/02 - Song.mp3"]
        self.assertEqual((song["title"], song["track"], song["artist"]), ("Song", 2, "Band"))
        self.assertEqual(song["cover"], "Music/Band/Album/cover.jpg")
        video = by_path["Uploader/A Video [abcDEF12345].mp4"]
        self.assertEqual((video["title"], video["source"], video["kind"]), ("A Video", "youtube", "video"))
        self.assertEqual(by_path["Garden Talk/Ep One [245be930].mp3"]["source"], "podcast")
        self.assertEqual(by_path["Web/Vimeo/Someone/Talk [42].webm"]["source"], "web")
        self.assertEqual(by_path["loose.mp3"]["source"], "other")
        self.assertEqual(lib["counts"], {"youtube": 1, "podcast": 1, "music": 2, "web": 1, "other": 1})
        self.assertEqual(self.index.ids(), {"vid"})

    def test_search_filter_sort_and_paging(self):
        L = lambda **kw: catalog.list_library(self.root, self.index, **kw)
        self.assertEqual([i["path"] for i in L(query="garden")["items"]], ["Garden Talk/Ep One [245be930].mp3"])
        self.assertEqual(L(source="music")["total"], 2)
        self.assertEqual(L(sort="size")["items"][0]["path"], "Uploader/A Video [abcDEF12345].mp4")
        self.assertEqual([i["title"] for i in L(sort="collection", source="music")["items"]],
                         ["01 - Intro", "02 - Song"])
        page = L(sort="title", offset=2, limit=2)
        self.assertEqual((len(page["items"]), page["total"]), (2, 6))

    def test_resolve_rel_refuses_escapes(self):
        outside = self.root.parent / "outside.mp3"
        outside.write_bytes(b"x")
        for bad in ("../outside.mp3", "/etc/passwd", "", "notes.txt", "Garden Talk", "a\x00b.mp3"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                catalog.resolve_rel(self.root, bad)
        with self.assertRaises(FileNotFoundError):
            catalog.resolve_rel(self.root, "missing.mp3")
        link = self.root / "link.mp3"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks not available")
        with self.assertRaises(ValueError):                   # a symlink pointing outside
            catalog.resolve_rel(self.root, "link.mp3")

    def test_delete_removes_sidecars_archive_entry_and_empty_folders(self):
        video = self.root / "Uploader" / "A Video [abcDEF12345].mp4"
        (video.parent / "A Video [abcDEF12345].en.srt").write_text("subs")
        (video.parent / "A Video [abcDEF12345].info.json").write_text("{}")
        (self.root / "archive.txt").write_text("youtube abcDEF12345\nyoutube keepme12345\n")
        result = catalog.delete(self.root, "Uploader/A Video [abcDEF12345].mp4", self.index)
        self.assertEqual(len(result["removed"]), 3)
        self.assertTrue(result["archive_cleared"])
        self.assertEqual((self.root / "archive.txt").read_text(), "youtube keepme12345\n")
        self.assertFalse((self.root / "Uploader").exists())          # pruned
        # An album folder with only its cover left is pruned too.
        catalog.delete(self.root, "Music/Band/Album/01 - Intro.mp3", self.index)
        self.assertTrue((self.root / "Music/Band/Album").exists())
        catalog.delete(self.root, "Music/Band/Album/02 - Song.mp3", self.index)
        self.assertFalse((self.root / "Music").exists())
        with self.assertRaises(ValueError):
            catalog.delete(self.root, "Garden Talk/cover.jpg", self.index)   # images aren't deletable

    def test_index_survives_reload_and_forgets(self):
        path = support.tmpdir() / "lib.json"
        idx = catalog.LibraryIndex(path)
        idx.record(self.root, [{"path": str(self.root / "loose.mp3")}, {"path": "/elsewhere.mp3"}],
                   {"source": "music", "video_id": "x", "title": "Loose"})
        again = catalog.LibraryIndex(path)
        self.assertEqual(set(again.snapshot()), {"loose.mp3"})          # outside paths ignored
        self.assertEqual(again.get("loose.mp3")["title"], "Loose")
        again.forget("loose.mp3")
        self.assertIsNone(catalog.LibraryIndex(path).get("loose.mp3"))


class FeedTests(unittest.TestCase):
    def setUp(self):
        catalog.invalidate()
        self.root = support.tmpdir()
        _populate(self.root)
        self.index = catalog.LibraryIndex(support.tmpdir() / "library.json")
        self.index.record(self.root, [{"path": str(self.root / "Garden Talk/Ep One [245be930].mp3"),
                                       "title": "Ep <One> & Two"}],
                          {"source": "podcast", "video_id": "guid-1", "artist": "Host",
                           "date": "2025-06-10", "duration": 3725})

    def test_archived_shows(self):
        shows = feeds.archived_shows(self.root, self.index)
        self.assertEqual(shows, [{"name": "Garden Talk", "slug": "Garden%20Talk", "episodes": 1,
                                  "cover": "Garden Talk/cover.jpg"}])

    def test_rss_is_valid_and_escaped(self):
        xml = feeds.podcast_rss(self.root, "Garden Talk", self.index, "http://127.0.0.1:8765/")
        root = ET.fromstring(xml)                                         # well-formed despite <, &
        item = root.find("channel/item")
        self.assertEqual(item.findtext("title"), "Ep <One> & Two")
        self.assertEqual(item.findtext("guid"), "guid-1")
        self.assertEqual(item.findtext("pubDate"), "Tue, 10 Jun 2025 00:00:00 GMT")
        enc = item.find("enclosure")
        self.assertTrue(enc.get("url").startswith("http://127.0.0.1:8765/api/library/file?path=Garden%20Talk%2F"))
        self.assertEqual((enc.get("length"), enc.get("type")), ("2000", "audio/mpeg"))
        self.assertEqual(item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"), "1:02:05")
        self.assertIsNone(feeds.podcast_rss(self.root, "Uploader", self.index, "http://x/"))

    def test_opml_roundtrip(self):
        follows = [{"feed_url": "https://a/feed", "name": "A & B"}, {"feed_url": "http://c/rss", "name": "C"}]
        parsed = feeds.opml_import(feeds.opml_export(follows))
        self.assertEqual(parsed, follows)

    def test_opml_import_variants(self):
        nested = """<opml version="1.0"><body><outline text="Podcasts">
          <outline text="X" xmlUrl="https://x/feed"/><outline title="Y" xmlurl="https://y/feed"/>
          <outline text="bad" xmlUrl="file:///etc/passwd"/><outline text="dup" xmlUrl="https://x/feed"/>
        </outline></body></opml>"""
        self.assertEqual([s["feed_url"] for s in feeds.opml_import(nested)], ["https://x/feed", "https://y/feed"])
        for bad in ("<rss/>", "not xml", '<!DOCTYPE x [<!ENTITY a "b">]><opml/>'):
            with self.assertRaises(ValueError):
                feeds.opml_import(bad)


def _feed(*eps):
    return {"podcast": "Show", "author": "Host", "artwork": "https://a/art.jpg", "episodes": [
        {"video_id": g, "url": f"https://cdn/{g}.mp3", "title": f"Ep {g}", "upload_date": d,
         "collection": "Show", "author": "Host", "artwork": "https://a/art.jpg"} for g, d in eps]}


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.m = make_manager()

    def test_follow_unfollow_import(self):
        subscriptions.follow(self.m, "https://a/feed", "A", since="20250101")
        subscriptions.follow(self.m, "https://a/feed", "A again")             # re-follow replaces
        self.assertEqual([f["name"] for f in subscriptions.follows(self.m)], ["A again"])
        with self.assertRaises(ValueError):
            subscriptions.follow(self.m, "file:///x")
        added = subscriptions.import_opml(self.m, feeds.opml_export(
            [{"feed_url": "https://a/feed", "name": "A"}, {"feed_url": "https://b/feed", "name": "B"}]))
        self.assertEqual(added, 1)
        subscriptions.unfollow(self.m, "https://a/feed")
        self.assertEqual([f["feed_url"] for f in subscriptions.follows(self.m)], ["https://b/feed"])

    def test_new_episodes_since_following(self):
        eps = subscriptions.new_episodes(_feed(("old", "20240101"), ("new", "20250301"), ("nodate", "")),
                                         "20250101", have=set())
        self.assertEqual([e["video_id"] for e in eps], ["new"])

    def test_sync_queues_only_new_unqueued_episodes(self):
        subscriptions.follow(self.m, "https://a/feed", "Show", since="20250101")
        feed = _feed(("old", "20240101"), ("n1", "20250301"), ("n2", "20250302"))
        with mock.patch.object(podcasts, "load_feed", return_value=feed) as load:
            first = subscriptions.sync(self.m)
            again = subscriptions.sync(self.m)
        self.assertTrue(load.call_args.kwargs["fresh"])                   # always re-fetched
        self.assertEqual((first["added"], again["added"]), (2, 0))
        queued = self.m.snapshot()
        self.assertEqual(sorted(i["video_id"] for i in queued), ["n1", "n2"])
        self.assertEqual(queued[0]["extra"]["tags"]["artist"], "Host")      # tagged from the feed
        self.assertGreater(self.m.settings_copy().podcast_last_sync, 0)

    def test_sync_reports_feed_errors(self):
        subscriptions.follow(self.m, "https://a/feed", "Broken")
        with mock.patch.object(podcasts, "load_feed", side_effect=RuntimeError("404")):
            r = subscriptions.sync(self.m)
        self.assertEqual(r["errors"], {"Broken": "404"})

    def test_auto_sync_due_and_run(self):
        auto = subscriptions.AutoSync(self.m)
        self.assertFalse(auto.due())                                       # off by default
        subscriptions.follow(self.m, "https://a/feed", "Show", since="20250101")
        self.m.update_settings({"podcast_sync_hours": 6})
        self.assertTrue(auto.due())
        q = self.m.listen()
        with mock.patch.object(podcasts, "load_feed", return_value=_feed(("n1", "20250301"))):
            result = auto.run_once()
        self.assertEqual(result["added"], 1)
        self.assertTrue(self.m.is_running())                               # started the queue
        events = [json.loads(q.get_nowait())["type"] for _ in range(q.qsize())]
        self.assertIn("notice", events)
        self.assertFalse(auto.due())                                       # just synced
        self.assertTrue(auto.due(now=time.time() + 7 * 3600))
        self.m.pause()


class ManagerLibraryTests(unittest.TestCase):
    def test_finished_album_is_recorded_track_by_track(self):
        m = make_manager(start_dispatcher=True)
        self.addCleanup(m.shutdown)
        folder = m.download_dir / "Music" / "Band" / "Album"
        q = m.listen()
        with mock.patch.object(m, "build_command", return_value=fake_command("tracks", str(folder))):
            (it,) = m.add([{"video_id": "alb", "url": "https://y/alb", "title": "Band - Album",
                            "artist": "Band", "album": "Album", "music_mode": "album"}], None, [],
                          source="music")
            m.start()
            wait_for(lambda: status_of(m, it.id) == "completed")
        item = m.item(it.id)
        self.assertEqual(item["output_file"], str(folder))                # several tracks → their folder
        self.assertEqual(item["total_bytes"], 1000.0)
        self.assertEqual(item["quality"], "mp3")                           # music format from settings
        lib = catalog.list_library(m.download_dir, m.library)
        tracks = sorted((i["track"], i["title"], i["collection"]) for i in lib["items"] if i["source"] == "music")
        self.assertEqual(tracks, [(1, "One", "Album"), (2, "Two", "Album")])
        self.assertIn("alb", m.downloaded_ids())
        types = [json.loads(q.get_nowait())["type"] for _ in range(q.qsize())]
        self.assertIn("library", types)


class LibraryApiTests(unittest.TestCase):
    def setUp(self):
        catalog.invalidate()
        self.m = make_manager()
        _populate(self.m.download_dir)
        self.client = create_app(self.m).test_client()

    def test_list_and_filters(self):
        d = self.client.get("/api/library?source=music&sort=title&limit=1").get_json()
        self.assertEqual((d["total"], len(d["items"])), (2, 1))
        d = self.client.get("/api/library?source=bogus&sort=bogus&offset=-5").get_json()
        self.assertEqual(d["total"], 6)

    def test_file_serving_supports_ranges_and_refuses_escapes(self):
        r = self.client.get("/api/library/file?path=loose.mp3", headers={"Range": "bytes=0-9"})
        try:
            self.assertEqual((r.status_code, len(r.data)), (206, 10))
        finally:
            r.close()
        self.assertEqual(self.client.get("/api/library/file?path=../../etc/passwd").status_code, 400)
        self.assertEqual(self.client.get("/api/library/file?path=missing.mp3").status_code, 404)
        self.assertEqual(self.client.get("/api/library/file?path=notes.txt").status_code, 400)

    def test_delete(self):
        r = self.client.post("/api/library/delete", json={"path": "loose.mp3"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.m.download_dir / "loose.mp3").exists())
        self.assertEqual(self.client.post("/api/library/delete", json={"path": "loose.mp3"}).status_code, 404)
        self.assertEqual(self.client.post("/api/library/delete", json={"path": "../x.mp3"}).status_code, 400)
        # Deleting must be a same-origin JSON request like every other write.
        self.assertEqual(self.client.post("/api/library/delete", data={"path": "x"}).status_code, 415)

    def test_feeds(self):
        shows = self.client.get("/api/library/shows").get_json()["shows"]
        self.assertEqual(shows[0]["feed_url"], "http://localhost/feeds/Garden%20Talk.xml")
        r = self.client.get("/feeds/Garden%20Talk.xml")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/rss+xml"))
        self.assertEqual(self.client.get("/feeds/Nope.xml").status_code, 404)
        self.assertEqual(self.client.get("/feeds/..%2Fx.xml").status_code, 404)

    def test_follow_endpoints_and_opml(self):
        r = self.client.post("/api/podcast/follow", json={"feed_url": "https://a/feed", "name": "A"})
        self.assertEqual(r.get_json()["follows"][0]["name"], "A")
        self.assertEqual(self.client.post("/api/podcast/follow", json={"feed_url": "ftp://x"}).status_code, 400)
        opml = self.client.get("/api/podcast/opml")
        self.assertIn("attachment", opml.headers["Content-Disposition"])
        self.assertIn(b"https://a/feed", opml.data)
        r = self.client.post("/api/podcast/opml", json={"opml": opml.data.decode().replace("a/feed", "b/feed")})
        self.assertEqual(r.get_json()["added"], 1)
        self.assertEqual(self.client.post("/api/podcast/opml", json={"opml": "<x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/podcast/opml", json={}).status_code, 400)
        self.assertEqual(len(self.client.get("/api/podcast/follows").get_json()["follows"]), 2)
        with mock.patch.object(subscriptions, "sync", return_value={"added": 0, "shows": {}, "errors": {}}):
            self.assertEqual(self.client.post("/api/podcast/sync", json={}).get_json()["added"], 0)
        r = self.client.post("/api/podcast/unfollow", json={"feed_url": "https://a/feed"})
        self.assertEqual(len(r.get_json()["follows"]), 1)

    def test_settings_validation_for_new_fields(self):
        self.assertEqual(self.client.post("/api/settings", json={"podcast_sync_hours": 5}).status_code, 400)
        s = self.client.post("/api/settings", json={"podcast_sync_hours": 6, "music_format": "m4a",
                                                    "podcast_follows": [{"feed_url": "https://evil"}]}).get_json()
        self.assertEqual((s["podcast_sync_hours"], s["music_format"], s["podcast_follows"]), (6, "m4a", []))

    def test_resolve_endpoints(self):
        from chive import music, web
        with mock.patch.object(music, "resolve", return_value={"kind": "single", "videos": [{"video_id": "x"}]}):
            r = self.client.post("/api/music/resolve", json={"input": "https://y/x"}).get_json()
        self.assertEqual(r["videos"][0]["downloaded"], False)
        self.assertEqual(self.client.post("/api/web/resolve", json={"url": "nope"}).status_code, 400)
        with mock.patch.object(web, "resolve", side_effect=RuntimeError("unsupported site")):
            r = self.client.post("/api/web/resolve", json={"url": "https://x.com/v"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (502, "unsupported site"))


if __name__ == "__main__":
    unittest.main()
