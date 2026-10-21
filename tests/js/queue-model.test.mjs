import { test } from "node:test";
import assert from "node:assert/strict";

import {
  applyQueueEvent, isIndeterminate, queueStats, queuedVideoIds, statusText,
} from "../../static/js/queue-model.js";

const item = (id, status = "pending", extra = {}) => ({ id, video_id: `v-${id}`, status, ...extra });

test("snapshot replaces the queue", () => {
  const r = applyQueueEvent([item("old")], { type: "snapshot", items: [item("a"), item("b")] });
  assert.deepEqual(r.queue.map((x) => x.id), ["a", "b"]);
  assert.equal(r.redraw, "all");
});

test("a batched queued event appends once and upserts duplicates", () => {
  const q = [item("a")];
  const r = applyQueueEvent(q, { type: "queued", items: [item("b"), item("a", "paused"), item("c")] });
  assert.deepEqual(r.queue.map((x) => x.id), ["a", "b", "c"]);
  assert.equal(r.queue[0].status, "paused");
  assert.equal(q.length, 1, "input is not mutated");
});

test("legacy single-item queued event still works", () => {
  const r = applyQueueEvent([], { type: "queued", item: item("a") });
  assert.deepEqual(r.queue.map((x) => x.id), ["a"]);
});

test("progress patches one row; status changes are flagged", () => {
  const q = [item("a", "downloading", { progress: 10 }), item("b")];
  let r = applyQueueEvent(q, { type: "progress", item: item("a", "downloading", { progress: 40 }) });
  assert.equal(r.redraw, "item");
  assert.equal(r.statusChanged, false);
  assert.equal(r.queue[0].progress, 40);
  r = applyQueueEvent(r.queue, { type: "update", item: item("a", "completed") });
  assert.equal(r.statusChanged, true);
});

test("an update for an unknown item is added", () => {
  const r = applyQueueEvent([], { type: "update", item: item("z", "downloading") });
  assert.equal(r.redraw, "all");
  assert.equal(r.queue.length, 1);
});

test("removed and cleared drop rows without a full redraw", () => {
  const q = [item("a", "completed"), item("b"), item("c", "failed")];
  let r = applyQueueEvent(q, { type: "removed", id: "b" });
  assert.deepEqual(r.ids, ["b"]);
  assert.equal(r.redraw, "removed");
  r = applyQueueEvent(q, { type: "cleared", ids: ["a", "c"] });
  assert.deepEqual(r.queue.map((x) => x.id), ["b"]);
  // Older servers sent no ids: fall back to every finished item.
  r = applyQueueEvent(q, { type: "cleared" });
  assert.deepEqual(r.ids, ["a", "c"]);
});

test("non-queue events change nothing", () => {
  const q = [item("a")];
  for (const p of [{ type: "settings" }, { type: "state" }, {}, null]) {
    const r = applyQueueEvent(q, p);
    assert.equal(r.queue, q);
    assert.equal(r.redraw, null);
  }
});

test("queuedVideoIds counts only active items", () => {
  const ids = queuedVideoIds([item("a"), item("b", "downloading"), item("c", "paused"), item("d", "completed")]);
  assert.deepEqual([...ids].sort(), ["v-a", "v-b", "v-c"]);
});

test("queueStats", () => {
  const s = queueStats([
    item("a", "downloading", { speed_bps: 100, downloaded_bytes: 300, total_bytes: 1300 }),
    item("b", "downloading", { speed_bps: 100, downloaded_bytes: 50, total_bytes: null }),
    item("c", "pending"), item("d", "paused"),
    item("e", "completed", { total_bytes: 1000 }),
    item("f", "failed"),
  ]);
  assert.equal(s.remaining, 4);
  assert.equal(s.done, 1);
  assert.equal(s.failed, 1);
  assert.equal(s.speed, 200);
  assert.equal(s.archived, 1350);
  assert.equal(s.eta, 5);               // 1000 bytes left at 200 B/s
  assert.equal(queueStats([]).eta, null);
});

test("statusText", () => {
  assert.equal(statusText(item("a")), "Queued");
  assert.equal(statusText(item("a", "downloading", { message: "Merging streams" })), "Merging streams");
  assert.equal(statusText(item("a", "completed", { message: "Saved" })), "Completed");
  assert.equal(statusText(item("a", "completed", { message: "Skipped — already in archive" })),
    "Completed · Skipped — already in archive");
  assert.equal(statusText(item("a", "failed", { message: "boom" })), "Failed · boom");
  assert.equal(statusText(item("a", "failed")), "Failed ·");
});

test("isIndeterminate", () => {
  const phases = new Set(["Merging streams"]);
  assert.equal(isIndeterminate(item("a", "downloading", { message: "Merging streams" }), phases), true);
  assert.equal(isIndeterminate(item("a", "downloading", { message: "Downloading video" }), phases), false);
  assert.equal(isIndeterminate(item("a", "completed", { message: "Merging streams" }), phases), false);
});
