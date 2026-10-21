import { test } from "node:test";
import assert from "node:assert/strict";

import { fmtBytes, fmtClock, fmtDate, fmtDuration, fmtViews, plural, safeFileName } from "../../static/js/format.js";

test("fmtDuration", () => {
  assert.equal(fmtDuration(null), "—");
  assert.equal(fmtDuration(NaN), "—");
  assert.equal(fmtDuration(0), "0:00");
  assert.equal(fmtDuration(65), "1:05");
  assert.equal(fmtDuration(3723), "1:02:03");
  assert.equal(fmtDuration(-5), "0:00");
});

test("fmtDate", () => {
  assert.equal(fmtDate("20250610"), "2025·06·10");
  for (const bad of [null, "", "2025", "2025-06-10", 20250610]) assert.equal(fmtDate(bad), "");
});

test("fmtViews", () => {
  assert.equal(fmtViews(999), "999");
  assert.equal(fmtViews(1500), "1.5K");
  assert.equal(fmtViews(2_300_000), "2.3M");
});

test("fmtBytes", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(null), "0 B");
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(1536), "1.5 KiB");
  assert.equal(fmtBytes(5 * 1024 ** 3), "5.0 GiB");
});

test("fmtClock", () => {
  assert.equal(fmtClock(null), "—");
  assert.equal(fmtClock(Infinity), "—");
  assert.equal(fmtClock(42), "42s");
  assert.equal(fmtClock(125), "2m 5s");
  assert.equal(fmtClock(3720), "1h 2m");
});

test("plural", () => {
  assert.equal(plural(1, "episode"), "1 episode");
  assert.equal(plural(2, "episode"), "2 episodes");
  assert.equal(plural(0, "match", "matches"), "0 matches");
  assert.equal(plural(3, "category", "categories"), "3 categories");
});

test("safeFileName", () => {
  assert.equal(safeFileName("Ep 1: Hello / World"), "Ep_1_Hello_World");
  assert.equal(safeFileName(""), "log");
  assert.equal(safeFileName("x".repeat(100)).length, 60);
});
