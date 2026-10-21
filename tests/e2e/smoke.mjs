// End-to-end smoke test: the real server + a real browser + a real download.
//
//   npm install && npx playwright install chromium   (once)
//   npm run test:e2e
//
// Starts `python server.py` against throwaway state/download dirs and a local
// media server (no internet needed), then drives the UI in Chromium.

import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { chromium } from "playwright";

const ROOT = new URL("../../", import.meta.url).pathname;
const PYTHON = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const PORT = 8700 + Math.floor(Math.random() * 200);
const BASE = `http://127.0.0.1:${PORT}`;
const sandbox = mkdtempSync(join(tmpdir(), "chive-e2e-"));

// ---- a real MP3 (made with ffmpeg) on a slow local server, so a download is
// watchable and the library player can actually play it.
const mp3 = join(sandbox, "episode.mp3");
const made = spawnSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
  "-c:a", "libmp3lame", "-b:a", "256k", mp3]);
if (made.status !== 0) { console.error("ffmpeg is required for the e2e test"); process.exit(1); }
const media = readFileSync(mp3);
const mediaServer = createServer(async (req, res) => {
  res.writeHead(200, { "Content-Type": "audio/mpeg", "Content-Length": media.length });
  for (let i = 0; i < media.length; i += 32768) {
    if (!res.write(media.subarray(i, i + 32768))) await new Promise((r) => res.once("drain", r));
    await new Promise((r) => setTimeout(r, 80));
  }
  res.end();
});
await new Promise((r) => mediaServer.listen(0, "127.0.0.1", r));
const mediaUrl = `http://127.0.0.1:${mediaServer.address().port}/episode.mp3`;

// ---- the Chive server
const server = spawn(PYTHON, ["server.py"], {
  cwd: ROOT,
  env: { ...process.env, CHIVE_PORT: String(PORT), CHIVE_STATE_DIR: join(sandbox, "state"),
    CHIVE_DOWNLOAD_DIR: join(sandbox, "downloads") },
  stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (d) => { serverLog += d; });
server.stderr.on("data", (d) => { serverLog += d; });

async function waitForServer() {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${BASE}/api/health`)).ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("server did not start:\n" + serverLog);
}

const step = (name) => console.log(`  ✓ ${name}`);
let browser;
let failed = false;

try {
  await waitForServer();
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForSelector('#conn-pill[data-state="live"]');
  step("page loads and the live event stream connects");

  await page.click("#settings-btn");
  assert.equal(await page.locator("#quality-seg button").count(), 5);
  assert.equal(await page.locator("#sb-grid .check").count(), 8);
  assert.match(await page.locator("#quality-hint").innerText(), /1080p/);
  // Focus is trapped inside the dialog…
  for (let i = 0; i < 40; i++) await page.keyboard.press("Tab");
  assert.ok(await page.evaluate(() => document.querySelector("#settings-modal").contains(document.activeElement)));
  // …and Escape closes it, returning focus to the gear.
  await page.keyboard.press("Escape");
  assert.ok(await page.locator("#settings-modal").isHidden());
  assert.equal(await page.evaluate(() => document.activeElement.id), "settings-btn");
  step("settings render from the server; the modal traps focus and restores it");

  // The cross-site guard: a non-JSON POST is refused even from our own page.
  const status = await page.evaluate(async () =>
    (await fetch("/api/queue/start", { method: "POST", body: "x", headers: { "Content-Type": "text/plain" } })).status);
  assert.equal(status, 415);
  step("non-JSON state-changing requests are refused");

  // Queue a podcast episode from the local media server and start the queue.
  await page.evaluate(async (url) => {
    const post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
    await post("/api/queue/add", { source: "podcast",
      videos: [{ video_id: "e2e-1", url, title: "Smoke Test Episode", collection: "E2E Show",
        author: "Chive QA", upload_date: "20250610" }] });
  }, mediaUrl);
  await page.waitForSelector(".qitem");
  await page.click("#q-start");
  await page.waitForSelector('.qitem[data-status="downloading"]', { timeout: 20000 });
  await page.waitForFunction(() => parseFloat(document.querySelector(".qchip-pct")?.textContent || "0") > 0);
  await page.waitForFunction(() => document.title.startsWith("↓"));
  step("queueing and starting a download streams live progress (also in the tab title)");

  const logBtn = page.locator('.qitem[data-status="downloading"] button[title="View log"]');
  await logBtn.click();
  await page.waitForSelector("#log-modal-live:not([hidden])");
  const before = (await page.locator("#log-modal-body").innerText()).length;
  await page.waitForFunction((n) => document.querySelector("#log-modal-body").innerText.length > n, before,
    { timeout: 15000 });
  assert.match(await page.locator("#log-modal-body").innerText(), /\[progress\]/);
  step("the log window follows the running download live");

  await page.waitForSelector('.qitem[data-status="completed"]', { timeout: 30000 });
  await page.waitForSelector("#log-modal-live", { state: "hidden" });
  assert.match(await page.locator("#log-modal-body").innerText(), /=== completed: Saved/);
  await page.keyboard.press("Escape");
  assert.ok(await page.locator("#log-modal").isHidden());
  step("the download completes and the log records the outcome");

  assert.equal(await page.locator("#stat-done").innerText(), "1");
  const before2 = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  await page.click("#theme-toggle");
  assert.notEqual(await page.evaluate(() => document.documentElement.getAttribute("data-theme")), before2);
  step("stats update and the theme toggles");

  // ---- the library: browse, search, play, delete
  await page.click("#tab-library");
  await page.waitForSelector('.lib-card[data-path^="E2E Show/"]');
  assert.match(await page.locator(".lib-card .lib-meta").first().innerText(), /Chive QA · E2E Show/);
  assert.equal(await page.locator("#library-count").innerText(), "1");
  await page.fill("#lib-search", "no such thing");
  await page.waitForSelector("#lib-empty:not([hidden])");
  await page.fill("#lib-search", "smoke");
  await page.waitForSelector(".lib-card");
  step("the library lists the download with its tags and searches it");

  await page.click(".lib-card .lib-art");
  await page.waitForSelector("#player-modal:not([hidden]) audio");
  await page.waitForFunction(() => document.querySelector("#player-body audio").currentTime > 0.2, null,
    { timeout: 10000 });
  await page.keyboard.press("Escape");
  assert.ok(await page.locator("#player-modal").isHidden());
  assert.equal(await page.locator("#player-body audio").count(), 0, "player torn down on close");
  step("playing from the library works, and closing stops it");

  page.once("dialog", (d) => d.accept());
  await page.click('.lib-card button[title="Delete from disk"]');
  await page.waitForSelector("#lib-empty:not([hidden])");
  assert.equal(await page.locator(".lib-card").count(), 0);
  step("deleting removes it from disk and the library");

  // ---- the other modes, and the "/" shortcut
  await page.click("#tab-archive");
  for (const [mode, re] of [["music", /album/i], ["web", /video or playlist/i], ["podcast", /podcasts/i]]) {
    await page.click(`#mode-seg button[data-mode="${mode}"]`);
    assert.match(await page.locator("#channel-url").getAttribute("placeholder"), re);
  }
  assert.ok(await page.locator("#follow-strip").isVisible(), "the Following strip shows in podcast mode");
  await page.click("#q-count");
  await page.keyboard.press("/");
  assert.equal(await page.evaluate(() => document.activeElement.id), "channel-url");
  step("music / any-site / podcast modes switch, and / focuses search");

  assert.deepEqual(pageErrors, [], "no uncaught page errors");
  step("no uncaught errors in the page");
} catch (err) {
  failed = true;
  console.error("\n✗ e2e failed:", err.message || err);
  console.error("\n--- server output ---\n" + serverLog);
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  await new Promise((r) => { server.once("exit", r); setTimeout(r, 8000); });
  mediaServer.close();
  rmSync(sandbox, { recursive: true, force: true });
}

if (failed) process.exit(1);
console.log("\ne2e smoke test passed");
