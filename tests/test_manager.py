"""The download queue: mutations, persistence, events, logs and the real
download lifecycle (driven by tests/fake_ytdlp.py instead of yt-dlp)."""

import json
import subprocess
import sys
import unittest
from unittest import mock

import support
from support import fake_command, make_manager, status_of, wait_for

from chive import config
from chive import manager as manager_mod
from chive.manager import QueueManager
from chive.models import QueueItem


def _videos(*ids):
    return [{"video_id": i, "title": f"Title {i}"} for i in ids]


def _drain(q):
    out = []
    while not q.empty():
        out.append(json.loads(q.get_nowait()))
    return out


class QueueMutationTests(unittest.TestCase):
    def setUp(self):
        self.m = make_manager()

    def test_add_dedupes_and_validates(self):
        added = self.m.add(_videos("a", "b", "a"), "1080p", ["sponsor", "bogus"])
        self.assertEqual([i.video_id for i in added], ["a", "b"])
        self.assertEqual(added[0].sponsorblock, ["sponsor"])
        self.assertEqual(self.m.add(_videos("a"), "1080p", []), [])       # still queued
        with self.assertRaises(ValueError):
            self.m.add(_videos("c"), "8k", [])
        self.assertEqual(self.m.add([{"title": "no id"}, "junk"], "1080p", []), [])

    def test_completed_items_may_be_queued_again(self):
        (it,) = self.m.add(_videos("a"), "1080p", [])
        self.m._items[it.id].status = "completed"
        self.assertEqual(len(self.m.add(_videos("a"), "1080p", [])), 1)

    def test_podcast_items_ignore_quality_and_sponsorblock(self):
        (it,) = self.m.add([{"video_id": "g", "url": "http://x/e.mp3", "collection": "Show"}],
                           None, ["sponsor"], source="podcast")
        self.assertEqual(it.quality, config.PODCAST_QUALITY)
        self.assertEqual(it.sponsorblock, [])
        self.assertEqual(it.collection, "Show")
        # A podcast episode without a URL can't be downloaded.
        self.assertEqual(self.m.add([{"video_id": "h"}], None, [], source="podcast"), [])

    def test_add_remembers_choices(self):
        self.m.add(_videos("a"), "720p", ["intro"], sb_mode="mark")
        s = self.m.settings()
        self.assertEqual((s["last_quality"], s["last_sponsorblock"], s["last_sb_mode"]),
                         ("720p", ["intro"], "mark"))

    def test_one_event_per_batch(self):
        q = self.m.listen()
        self.m.add(_videos(*[f"v{i}" for i in range(5)]), "1080p", [])
        queued = [e for e in _drain(q) if e["type"] == "queued"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(len(queued[0]["items"]), 5)

    def test_remove_retry_clear(self):
        a, b, c = self.m.add(_videos("a", "b", "c"), "1080p", [])
        self.m._items[a.id].status = "downloading"
        self.assertFalse(self.m.remove(a.id))            # can't remove mid-download
        self.assertTrue(self.m.remove(b.id))
        self.assertFalse(self.m.remove("nope"))
        self.assertFalse(self.m.retry(c.id))              # only failed/cancelled
        self.m._items[c.id].status = "failed"
        self.assertTrue(self.m.retry(c.id))
        self.assertEqual(status_of(self.m, c.id), "pending")
        self.m._items[a.id].status = "completed"
        q = self.m.listen()
        self.assertEqual(self.m.clear_finished(), 1)
        cleared = [e for e in _drain(q) if e["type"] == "cleared"]
        self.assertEqual(cleared[0]["ids"], [a.id])
        self.assertEqual([i["id"] for i in self.m.snapshot()], [c.id])

    def test_pause_and_resume_pending(self):
        (a,) = self.m.add(_videos("a"), "1080p", [])
        self.assertTrue(self.m.pause_item(a.id))
        self.assertEqual(status_of(self.m, a.id), "paused")
        self.assertFalse(self.m.pause_item(a.id))
        self.assertTrue(self.m.resume_item(a.id))
        self.assertEqual(status_of(self.m, a.id), "pending")
        self.assertFalse(self.m.resume_item(a.id))

    def test_settings_update_validates_and_broadcasts(self):
        q = self.m.listen()
        changed = self.m.update_settings({"concurrency": 3, "embed_thumbnail": False})
        self.assertEqual(changed, {"concurrency": 3, "embed_thumbnail": False})
        events = [e for e in _drain(q) if e["type"] == "settings"]
        self.assertEqual(events[0]["concurrency"], 3)
        with self.assertRaises(ValueError):
            self.m.update_settings({"concurrency": "many"})
        self.assertEqual(self.m.settings()["concurrency"], 3)

    def test_bad_download_dir_is_rejected(self):
        blocker = support.tmpdir() / "file"
        blocker.write_text("x")
        with self.assertRaises(ValueError):
            self.m.update_settings({"download_dir": str(blocker / "sub")})

    def test_full_listener_keeps_newest_event(self):
        q = self.m.listen()
        for i in range(q.maxsize):
            q.put_nowait(f"old{i}")
        self.m._broadcast({"type": "update", "marker": "terminal"})
        drained = [q.get_nowait() for _ in range(q.qsize())]
        self.assertTrue(any("terminal" in x for x in drained))
        self.assertLessEqual(len(drained), q.maxsize)

    def test_downloaded_ids_cache_refreshes_after_completion(self):
        dl = self.m.download_dir
        (dl / "A [aaaaaaaaaaa].mp4").write_text("x")
        self.assertIn("aaaaaaaaaaa", self.m.downloaded_ids())
        (dl / "B [bbbbbbbbbbb].mp4").write_text("x")
        self.assertNotIn("bbbbbbbbbbb", self.m.downloaded_ids())   # cached scan
        it = support.make_item(id="z")
        self.m._end_item(it, "completed", "Saved")                  # library changed
        self.assertIn("bbbbbbbbbbb", self.m.downloaded_ids())


class PersistenceTests(unittest.TestCase):
    def test_state_roundtrip_and_restore(self):
        m = make_manager(concurrency=2, subtitles=True)
        a, b = m.add(_videos("a", "b"), "720p", ["sponsor"])
        m._items[a.id].status = "downloading"        # as if interrupted
        m._items[a.id].speed = "1 MiB/s"
        m._persist()
        m2 = QueueManager(state_file=m.state_file, item_log_dir=m.item_log_dir,
                          start_dispatcher=False)
        self.assertEqual(m2.settings()["concurrency"], 2)
        self.assertTrue(m2.settings()["subtitles"])
        restored = {i["video_id"]: i for i in m2.snapshot()}
        self.assertEqual(restored["a"]["status"], "pending")
        self.assertEqual(restored["a"]["message"], "Restored — will resume")
        self.assertIsNone(restored["a"]["speed"])
        self.assertEqual(restored["b"]["quality"], "720p")

    def test_corrupt_records_are_skipped(self):
        d = support.tmpdir()
        (d / "state.json").write_text(json.dumps({
            "concurrency": "x",
            "queue": [{"video_id": "ok", "url": "u", "status": "weird"},
                      {"title": "missing id"}, "junk",
                      {"video_id": "ok2", "url": "u", "progress": "NaNx"}],
        }))
        m = QueueManager(state_file=d / "state.json", item_log_dir=d / "logs",
                         start_dispatcher=False)
        self.assertEqual([i["video_id"] for i in m.snapshot()], ["ok"])
        self.assertEqual(m.snapshot()[0]["status"], "pending")
        self.assertEqual(m.settings()["concurrency"], 1)

    def test_item_from_dict_sanitises(self):
        it = QueueItem.from_dict({"video_id": "v", "url": "u", "sponsorblock": ["x", "intro"],
                                  "sponsorblock_mode": "?", "source": "??"}, "1080p")
        self.assertEqual((it.sponsorblock, it.sponsorblock_mode, it.source),
                         (["intro"], "remove", "youtube"))


class ItemLogTests(unittest.TestCase):
    def setUp(self):
        self.m = make_manager()

    def test_log_survives_a_restart(self):
        self.m._open_item_log("abc")
        self.m._log_line("abc", "hello")
        self.m._log_line("abc", "world")
        self.m._close_item_log("abc")
        fresh = QueueManager(state_file=self.m.state_file, item_log_dir=self.m.item_log_dir,
                             start_dispatcher=False)
        self.assertEqual(fresh.get_log("abc"), ["hello", "world"])
        fresh._delete_log_file("abc")
        self.assertIsNone(fresh.get_log("abc"))

    def test_unsafe_ids_never_become_paths(self):
        for bad in ("../../etc/passwd", "", "a/b", "x" * 200):
            self.assertIsNone(self.m._log_path(bad))
            self.assertIsNone(self.m.get_log(bad))

    def test_removing_an_item_deletes_its_log(self):
        (a,) = self.m.add(_videos("a"), "1080p", [])
        self.m._open_item_log(a.id)
        self.m._log_line(a.id, "x")
        path = self.m._log_path(a.id)
        self.assertTrue(path.exists())
        self.m.remove(a.id)
        self.assertFalse(path.exists())


class LifecycleTests(unittest.TestCase):
    """Real child processes through the real dispatcher/worker threads."""

    def setUp(self):
        self.m = make_manager(start_dispatcher=True)
        self.addCleanup(self.m.shutdown)
        self.modes = {}
        out_dir = self.m.download_dir

        def build(item):
            mode = self.modes.get(item.video_id, "ok")
            return fake_command(mode, str(out_dir / f"{item.video_id}.mp4")) if mode == "ok" \
                else fake_command(mode)
        patcher = mock.patch.object(self.m, "build_command", side_effect=build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, vid, mode="ok", until=("completed", "failed", "cancelled", "paused")):
        self.modes[vid] = mode
        (it,) = self.m.add(_videos(vid), "1080p", [])
        self.m.start()
        wait_for(lambda: status_of(self.m, it.id) in until)
        return self.m.item(it.id)

    def test_completed_download(self):
        q = self.m.listen()
        it = self._run("ok1")
        self.assertEqual((it["status"], it["message"], it["progress"]), ("completed", "Saved", 100.0))
        self.assertTrue(it["output_file"].endswith("ok1.mp4"))
        self.assertEqual(it["total_bytes"], 10000.0)
        types = [e["type"] for e in _drain(q)]
        self.assertIn("progress", types)
        log = self.m.get_log(it["id"])
        self.assertTrue(log[0].startswith("=== ") and "starting" in log[0])
        self.assertTrue(any(ln.startswith("[progress]") for ln in log))
        self.assertIn("[postprocess] Merger", log)
        self.assertFalse(any("[YTPROG]" in ln for ln in log))
        self.assertEqual(log[-1], "=== completed: Saved")

    def test_failed_download_keeps_the_error(self):
        it = self._run("bad1", "fail")
        self.assertEqual(it["status"], "failed")
        self.assertEqual(it["message"], "[youtube] v: Video unavailable")

    def test_archive_skip_is_reported(self):
        it = self._run("arc1", "archived")
        self.assertEqual((it["status"], it["message"]), ("completed", "Skipped — already in archive"))

    def test_cancel(self):
        self.modes["hang1"] = "hang"
        (it,) = self.m.add(_videos("hang1"), "1080p", [])
        self.m.start()
        wait_for(lambda: it.id in self.m._active_procs)
        self.assertTrue(self.m.cancel_item(it.id))
        wait_for(lambda: status_of(self.m, it.id) == "cancelled")
        self.assertFalse(self.m.cancel_item(it.id))

    def test_pause_running_then_resume_appends_to_log(self):
        self.modes["p1"] = "hang"
        (it,) = self.m.add(_videos("p1"), "1080p", [])
        self.m.start()
        wait_for(lambda: it.id in self.m._active_procs)
        self.assertTrue(self.m.pause_item(it.id))
        wait_for(lambda: status_of(self.m, it.id) == "paused")
        self.modes["p1"] = "ok"
        self.m.resume_item(it.id)
        wait_for(lambda: status_of(self.m, it.id) == "completed")
        log = self.m.get_log(it.id)
        self.assertEqual(sum("starting" in ln for ln in log if ln.startswith("===")), 1)
        self.assertEqual(sum("resuming" in ln for ln in log if ln.startswith("===")), 1)

    def test_concurrency_limit(self):
        self.modes.update({"c1": "hang", "c2": "hang"})
        a, b = self.m.add(_videos("c1", "c2"), "1080p", [])
        self.m.start()
        wait_for(lambda: a.id in self.m._active_procs)
        self.assertEqual(status_of(self.m, b.id), "pending")        # one slot
        self.m.update_settings({"concurrency": 2})
        wait_for(lambda: b.id in self.m._active_procs)

    def test_hold_stops_dispatching(self):
        self.m.pause()
        (it,) = self.m.add(_videos("held"), "1080p", [])
        self.m._wake.set()
        self.assertEqual(status_of(self.m, it.id), "pending")
        self.assertFalse(self.m.is_running())

    def test_low_disk_refuses_to_start(self):
        with mock.patch.object(manager_mod, "disk_free", return_value=10):
            it = self._run("disk1")
        self.assertEqual(it["status"], "failed")
        self.assertIn("Insufficient disk space", it["message"])

    def test_missing_binary(self):
        with mock.patch.object(self.m, "build_command", return_value=["/no/such/yt-dlp"]):
            it = self._run("nobin")
        self.assertEqual((it["status"], it["message"]), ("failed", "yt-dlp not found on PATH"))


class ShutdownTests(unittest.TestCase):
    def test_shutdown_stops_downloads_and_leaves_them_resumable(self):
        m = make_manager(start_dispatcher=True)
        with mock.patch.object(m, "build_command", return_value=fake_command("hang")):
            (it,) = m.add(_videos("s1"), "1080p", [])
            m.start()
            wait_for(lambda: it.id in m._active_procs)
            proc = m._active_procs[it.id]
            m.shutdown(timeout=5)
        self.assertIsNotNone(proc.poll())
        self.assertFalse(m.is_running())
        wait_for(lambda: "shutting down" in (m.get_log(it.id) or [""])[-1])
        restored = QueueManager(state_file=m.state_file, item_log_dir=m.item_log_dir,
                                start_dispatcher=False)
        self.assertEqual(restored.snapshot()[0]["status"], "pending")
        m.shutdown()                                   # idempotent

    def test_shutdown_kills_a_process_that_ignores_sigterm(self):
        m = make_manager()
        stubborn = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            **support_kwargs())
        m._active_procs["x"] = stubborn
        m.shutdown(timeout=0.5)
        stubborn.wait(timeout=5)
        self.assertIsNotNone(stubborn.poll())


def support_kwargs():
    from chive.util import detached_popen_kwargs
    return detached_popen_kwargs()


if __name__ == "__main__":
    unittest.main()
