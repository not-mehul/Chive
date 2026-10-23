"""Formatting helpers, the TTL cache, file helpers and the Settings model."""

import json
import time
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (sandboxes state before chive is imported)

from chive import config, util
from chive.settings import API_FIELDS, Settings


class FormattingTests(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(util.human_bytes(1536), "1.5 KiB")
        self.assertEqual(util.human_bytes(0), "0 B")
        self.assertEqual(util.human_bytes(None), "0 B")
        self.assertEqual(util.human_bytes(512), "512 B")
        self.assertEqual(util.human_bytes(3 * 1024 ** 3), "3.0 GiB")

    def test_fmt_eta(self):
        self.assertEqual(util.fmt_eta(7), "00:07")
        self.assertEqual(util.fmt_eta(3661), "1:01:01")
        self.assertIsNone(util.fmt_eta(None))
        self.assertIsNone(util.fmt_eta(-1))


class TTLCacheTests(unittest.TestCase):
    def test_expiry(self):
        c = util.TTLCache(ttl=0.05, maxsize=4)
        c.set("a", 1)
        self.assertEqual(c.get("a"), 1)
        time.sleep(0.08)
        self.assertIsNone(c.get("a"))

    def test_lru_eviction_keeps_it_bounded(self):
        c = util.TTLCache(ttl=60, maxsize=2)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")            # a is now most recently used
        c.set("c", 3)         # evicts b
        self.assertEqual(len(c), 2)
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("a"), 1)


class FileHelperTests(unittest.TestCase):
    def test_atomic_write_and_load_roundtrip(self):
        path = support.tmpdir() / "nested" / "s.json"
        util.atomic_write_json(path, {"a": 1, "p": Path("/x")})
        self.assertEqual(util.load_json(path), {"a": 1, "p": "/x"})
        self.assertEqual([p.name for p in path.parent.iterdir()], ["s.json"])  # no temp left

    def test_load_json_tolerates_garbage(self):
        d = support.tmpdir()
        (d / "bad.json").write_text("{nope")
        (d / "list.json").write_text("[1, 2]")
        self.assertEqual(util.load_json(d / "bad.json"), {})
        self.assertEqual(util.load_json(d / "list.json"), {})
        self.assertEqual(util.load_json(d / "missing.json"), {})

    def test_atomic_write_cleans_up_on_failure(self):
        d = support.tmpdir()
        with mock.patch("chive.util.json.dump", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                util.atomic_write_json(d / "s.json", {"a": 1})
        self.assertEqual(list(d.iterdir()), [])

    def test_disk_free_walks_up_to_an_existing_parent(self):
        missing = support.tmpdir() / "not" / "yet" / "created"
        self.assertIsNotNone(util.disk_free(missing))
        self.assertIsNotNone(util.disk_total(missing))


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings()
        self.assertEqual(s.concurrency, 1)
        self.assertEqual(s.last_quality, config.DEFAULT_QUALITY)
        self.assertTrue(s.subtitle_embed)

    def test_from_dict_drops_invalid_values(self):
        s = Settings.from_dict({
            "concurrency": 99, "cookies_browser": "netscape", "last_quality": "8k",
            "last_sponsorblock": ["sponsor", "bogus"], "last_sb_mode": "explode",
            "subtitle_langs": "  ", "history": ["a", 3, "b"], "unknown": 1,
        })
        self.assertEqual(s.concurrency, config.MAX_CONCURRENCY)   # clamped
        self.assertEqual(s.cookies_browser, "")
        self.assertEqual(s.last_quality, config.DEFAULT_QUALITY)
        self.assertEqual(s.last_sponsorblock, ["sponsor"])
        self.assertEqual(s.last_sb_mode, "remove")
        self.assertEqual(s.subtitle_langs, "en")
        self.assertEqual(s.history, ["a", "b"])

    def test_update_reports_only_changes(self):
        s = Settings()
        self.assertEqual(s.update({"embed_metadata": True}), {})
        self.assertEqual(s.update({"embed_metadata": False, "concurrency": "3"}),
                         {"embed_metadata": False, "concurrency": 3})

    def test_update_rejects_malformed_and_applies_nothing(self):
        s = Settings()
        with self.assertRaises(ValueError):
            s.update({"embed_metadata": False, "concurrency": "lots"})
        self.assertEqual(s.concurrency, 1)

    def test_string_booleans(self):
        s = Settings()
        s.update({"use_archive": "false", "subtitles": "true"})
        self.assertFalse(s.use_archive)
        self.assertTrue(s.subtitles)

    def test_roundtrip_is_json_ready(self):
        s = Settings()
        s.update({"download_dir": "~/x", "history": ["u"]})
        d = s.to_dict()
        json.dumps(d)
        self.assertEqual(Settings.from_dict(d).to_dict(), d)

    def test_push_history_dedupes_and_caps(self):
        s = Settings()
        for i in range(config.HISTORY_MAX + 3):
            s.push_history(f"u{i}")
        s.push_history("u5")
        self.assertEqual(s.history[0], "u5")
        self.assertEqual(len(s.history), config.HISTORY_MAX)
        self.assertEqual(len(set(s.history)), len(s.history))

    def test_copy_is_independent(self):
        s = Settings()
        c = s.copy()
        c.history.append("x")
        self.assertEqual(s.history, [])

    def test_history_is_not_api_writable(self):
        self.assertNotIn("history", API_FIELDS)
        self.assertIn("download_dir", API_FIELDS)


if __name__ == "__main__":
    unittest.main()
