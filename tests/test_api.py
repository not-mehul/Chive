"""The HTTP API, against an isolated app + manager per test class."""

import subprocess
import unittest
from unittest import mock

import support
from support import make_manager

from chive import __version__, api, podcasts, youtube, ytdlp
from chive.api import allowed_hosts_for, create_app


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.m = make_manager()
        self.app = create_app(self.m)
        self.client = self.app.test_client()

    def post(self, path, body=None, **kw):
        return self.client.post(path, json=body if body is not None else {}, **kw)


class GuardTests(ApiTestCase):
    def test_foreign_host_is_refused(self):
        # DNS rebinding: the attacker's domain resolves to 127.0.0.1.
        r = self.client.get("/api/queue", headers={"Host": "evil.example:8765"})
        self.assertEqual(r.status_code, 403)
        for host in ("127.0.0.1:8765", "localhost", "[::1]:8765"):
            self.assertEqual(self.client.get("/api/queue", headers={"Host": host}).status_code, 200)

    def test_cross_origin_post_is_refused(self):
        r = self.post("/api/queue/start", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.m.is_running())
        r = self.post("/api/queue/start", headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)

    def test_non_json_post_is_refused(self):
        # A cross-site <form> can only send "simple" content types.
        r = self.client.post("/api/queue/start", data="x", content_type="text/plain")
        self.assertEqual(r.status_code, 415)
        r = self.client.post("/api/settings", data={"download_dir": "/tmp/x"})
        self.assertEqual(r.status_code, 415)

    def test_reads_need_no_json(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_allowed_hosts_policy(self):
        self.assertEqual(allowed_hosts_for("127.0.0.1"), api.LOOPBACK_HOSTS)
        self.assertIn("nas.local", allowed_hosts_for("127.0.0.1", "nas.local, other"))
        self.assertIsNone(allowed_hosts_for("0.0.0.0"))
        self.assertEqual(allowed_hosts_for("0.0.0.0", "nas.local"), frozenset({"nas.local"}))
        open_app = create_app(self.m, allowed_hosts=None).test_client()
        self.assertEqual(open_app.get("/api/queue", headers={"Host": "anything"}).status_code, 200)


class ErrorTests(ApiTestCase):
    def test_unknown_api_route_is_json(self):
        r = self.client.get("/api/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_oversized_body_is_refused(self):
        r = self.client.post("/api/podcast/opml", data=b"x" * (api.MAX_REQUEST_BYTES + 1),
                             content_type="application/json")
        self.assertEqual(r.status_code, 413)
        self.assertIn("error", r.get_json())

    def test_unexpected_exception_is_json(self):
        with mock.patch.object(self.m, "snapshot", side_effect=RuntimeError("kaboom")), \
                self.assertLogs(self.app.logger, "ERROR"):        # logged, not swallowed
            r = self.client.get("/api/queue")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"], "kaboom")

    def test_index_and_static(self):
        with self.client.get("/") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"Chive", r.data)
        with self.client.get("/static/js/app.js") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("javascript", r.content_type)    # ES modules need a JS type


class HealthTests(ApiTestCase):
    def test_health(self):
        with mock.patch.object(ytdlp, "version", return_value="2026.01.01"):
            h = self.client.get("/api/health").get_json()
        self.assertEqual(h["version"], __version__)
        self.assertEqual(h["yt_dlp_version"], "2026.01.01")
        self.assertIn("disk_free", h)

    def test_versions_are_cached(self):
        ytdlp.invalidate_versions()
        with mock.patch.object(ytdlp, "_probe", return_value="1.0") as probe, \
                mock.patch.object(ytdlp, "base_cmd", return_value=["yt-dlp"]):
            for _ in range(3):
                ytdlp.version()
            self.assertEqual(probe.call_count, 1)
            ytdlp.invalidate_versions()
            ytdlp.version()
            self.assertEqual(probe.call_count, 2)
        ytdlp.invalidate_versions()


class SourceEndpointTests(ApiTestCase):
    def test_scrape_validation(self):
        self.assertEqual(self.post("/api/scrape", {}).status_code, 400)
        self.assertEqual(self.post("/api/scrape", {"url": "ftp://x.com"}).status_code, 400)
        r = self.post("/api/scrape", {"url": "some channel name"}).get_json()
        self.assertEqual(r, {"needs_search": True, "query": "some channel name"})

    def test_malformed_numbers_fall_back(self):
        with mock.patch.object(youtube, "scrape_channel",
                               return_value={"videos": [], "page": 1}) as sc:
            r = self.post("/api/scrape", {"url": "@chan", "page": "abc", "duration_min": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sc.call_args[0][1], 1)
        self.assertIsNone(sc.call_args.kwargs["filters"]["duration_min"])

    def test_scrape_flags_downloaded_and_records_history(self):
        (self.m.download_dir / "X [vvvvvvvvvvv].mp4").write_text("x")
        result = {"videos": [{"video_id": "vvvvvvvvvvv"}, {"video_id": "other"}], "page": 1}
        with mock.patch.object(youtube, "scrape_channel", return_value=result):
            r = self.post("/api/scrape", {"url": "@chan"}).get_json()
        self.assertEqual([v["downloaded"] for v in r["videos"]], [True, False])
        self.assertEqual(self.m.settings()["history"], ["https://www.youtube.com/@chan"])

    def test_upstream_failure_is_502(self):
        with mock.patch.object(youtube, "scrape_channel", side_effect=RuntimeError("no channel")):
            r = self.post("/api/scrape", {"url": "@chan"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (502, "no channel"))

    def test_feed_url_must_be_http(self):
        r = self.post("/api/podcast/episodes", {"feed_url": "file:///etc/hostname"})
        self.assertEqual(r.status_code, 400)

    def test_episodes_flag_downloaded(self):
        page = {"podcast": "Show", "videos": [{"video_id": "g1", "title": "A"},
                                              {"video_id": "g2", "title": "B"}]}
        show = self.m.download_dir / "Show"
        show.mkdir()
        (show / "A.mp3").write_text("x")
        with mock.patch.object(podcasts, "fetch_episodes", return_value=page) as fe:
            r = self.post("/api/podcast/episodes", {"feed_url": "http://f", "page_size": 99999})
        self.assertEqual([v["downloaded"] for v in r.get_json()["videos"]], [True, False])
        self.assertEqual(fe.call_args.kwargs["page_size"], 5000)          # clamped

    def test_search_endpoints_require_a_query(self):
        self.assertEqual(self.post("/api/search", {}).status_code, 400)
        self.assertEqual(self.post("/api/podcast/search", {"query": " "}).status_code, 400)


class QueueEndpointTests(ApiTestCase):
    def test_add_and_lifecycle_endpoints(self):
        self.assertEqual(self.post("/api/queue/add", {"videos": []}).status_code, 400)
        self.assertEqual(self.post("/api/queue/add", {"videos": [{"video_id": "a"}],
                                                      "quality": "8k"}).status_code, 400)
        r = self.post("/api/queue/add", {"videos": [{"video_id": "a"}]}).get_json()
        item_id = r["added"][0]["id"]
        self.assertTrue(self.post("/api/queue/pause-item", {"id": item_id}).get_json()["ok"])
        self.assertTrue(self.post("/api/queue/resume-item", {"id": item_id}).get_json()["ok"])
        self.assertFalse(self.post("/api/queue/cancel", {"id": item_id}).get_json()["ok"])
        self.assertFalse(self.post("/api/queue/retry", {"id": item_id}).get_json()["ok"])
        self.assertTrue(self.post("/api/queue/remove", {"id": item_id}).get_json()["ok"])
        self.assertEqual(self.client.get("/api/queue").get_json()["items"], [])

    def test_item_actions_require_an_id(self):
        for path in ("remove", "retry", "cancel", "pause-item", "resume-item"):
            self.assertEqual(self.post(f"/api/queue/{path}", {}).status_code, 400)

    def test_start_pause_clear(self):
        self.assertTrue(self.post("/api/queue/start").get_json()["running"])
        self.assertFalse(self.post("/api/queue/pause").get_json()["running"])
        self.assertEqual(self.post("/api/queue/clear").get_json()["removed"], 0)

    def test_log_endpoint(self):
        self.assertEqual(self.client.get("/api/queue/log").status_code, 400)
        r = self.client.get("/api/queue/log?id=missing").get_json()
        self.assertEqual(r, {"lines": [], "available": False, "status": None})
        (a,) = self.m.add([{"video_id": "a"}], "1080p", [])
        self.m._log_line(a.id, "hello")
        r = self.client.get(f"/api/queue/log?id={a.id}").get_json()
        self.assertEqual(r, {"lines": ["hello"], "available": True, "status": "pending"})

    def test_reveal_creates_missing_destination(self):
        self.m.update_settings({"download_dir": str(support.tmpdir() / "new")})
        self.m.download_dir.rmdir()
        with mock.patch.object(api, "reveal_path") as reveal:
            r = self.post("/api/queue/reveal", {})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.m.download_dir.is_dir())
        reveal.assert_called_once()

    def test_event_stream(self):
        r = self.client.get("/api/events")
        try:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.mimetype, "text/event-stream")
            # waitress rejects hop-by-hop headers (PEP 3333): it killed the stream.
            self.assertNotIn("Connection", r.headers)
            first = next(r.response)
            self.assertTrue(first.startswith(b'data: {"type": "snapshot"'))
        finally:
            r.close()


class SettingsEndpointTests(ApiTestCase):
    def test_get_includes_catalogues(self):
        s = self.client.get("/api/settings").get_json()
        self.assertEqual([q["id"] for q in s["qualities"]], ["4k", "1080p", "720p", "m4a", "mp3"])
        self.assertTrue(all(q["hint"] for q in s["qualities"]))
        self.assertEqual(s["sponsorblock"][0], {"id": "sponsor", "label": "Sponsor"})
        self.assertIn("firefox", s["cookie_browsers"])

    def test_post_applies_and_validates(self):
        r = self.post("/api/settings", {"concurrency": 2, "subtitles": True, "history": ["x"]})
        s = r.get_json()
        self.assertEqual((s["concurrency"], s["subtitles"], s["history"]), (2, True, []))
        r = self.post("/api/settings", {"concurrency": "lots"})
        self.assertEqual(r.status_code, 400)

    def test_update_ytdlp_explains_pep668(self):
        out = mock.Mock(returncode=1, stdout="", stderr="error: externally-managed-environment")
        with mock.patch.object(ytdlp, "update_command", return_value=["pip"]), \
                mock.patch.object(api.subprocess, "run", return_value=out), \
                mock.patch.object(ytdlp, "version", return_value="x"):
            r = self.post("/api/tools/update-ytdlp").get_json()
        self.assertFalse(r["ok"])
        self.assertIn("virtual environment", r["hint"])

    def test_update_ytdlp_timeout(self):
        with mock.patch.object(ytdlp, "update_command", return_value=["pip"]), \
                mock.patch.object(api.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("pip", 1)):
            self.assertEqual(self.post("/api/tools/update-ytdlp").status_code, 504)


if __name__ == "__main__":
    unittest.main()
