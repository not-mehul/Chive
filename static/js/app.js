// Chive frontend — talks to the local bridge (server.py) over JSON + SSE.
//
// Module map: format.js (pure formatting) · queue-model.js (pure queue/event
// logic) · dom.js · api.js · modal.js · log-viewer.js · library.js ·
// notifications.js · eggs.js. This file owns the Archive view's state,
// rendering and bindings, and the view switching.

import { api, post } from "./api.js";
import { $, ICONS, byDataId, el, svgEl, toast } from "./dom.js";
import { initEasterEggs } from "./eggs.js";
import { fmtBytes, fmtClock, fmtDate, fmtDuration, fmtViews, plural } from "./format.js";
import { focusLibrarySearch, initLibrary, onLibraryChanged, showLibrary } from "./library.js";
import { initLogViewer, openLog, showStaticLog } from "./log-viewer.js";
import { closeModal, initModals, openModal } from "./modal.js";
import { initNotifications, notify, updateTitle } from "./notifications.js";
import { applyQueueEvent, isIndeterminate, queueStats, queuedVideoIds, statusText } from "./queue-model.js";

const EMPTY_FILTERS = { durationMin: "", durationMax: "", viewsMin: "", viewsMax: "", dateFrom: "", dateTo: "" };

const MODES = {
  youtube: { title: "Channel or playlist", placeholder: "URL, @handle, or a channel name to search…",
    filter: "Search titles across the whole channel…", queueAll: "Queue every video on this page",
    hint: "Paste a channel/playlist URL or an @handle, or type a name to search. Click a thumbnail to queue it." },
  podcast: { title: "Find a podcast", placeholder: "Search podcasts by name…",
    filter: "Search episode titles…", queueAll: "Queue every episode of this podcast",
    hint: "Search a podcast, pick a show, then click episodes to queue them — or queue the whole feed." },
  music: { title: "Albums & tracks", placeholder: "Album/playlist URL, or an album to search…",
    filter: "Filter these results…", queueAll: "Queue everything listed",
    hint: "Paste a YouTube Music, Bandcamp or SoundCloud album/playlist, or a full-album video — "
      + "or search. Full albums are split into tagged tracks by chapter." },
  web: { title: "Any site", placeholder: "Link to a video or playlist on any site yt-dlp supports…",
    filter: "Filter these results…", queueAll: "Queue everything listed",
    hint: "Vimeo, Twitch VODs, SoundCloud, news sites and more — anything yt-dlp supports. "
      + "Saved under Web/<site>/." },
};

const state = {
  mode: "youtube",    // youtube | podcast | music | web
  podcast: null,      // { name, feed_url, artwork } once a show is picked
  allVideos: [],      // music/web: the whole resolved list, paged client-side
  follows: [],
  musicFormats: [],
  musicFormat: "mp3",
  musicKeepFull: false,
  syncHours: 0,
  syncChoices: [0],
  remainingBefore: 0, // for the "queue finished" notification
  channelUrl: "",
  page: 1,
  filtered: false,
  ignoreShorts: true,
  videos: [],
  filter: "",
  sort: "newest",
  filters: { ...EMPTY_FILTERS },
  // Clicking a card queues/unqueues it directly, so "selected" == queued.
  queuedIds: new Set(),
  videoById: new Map(),   // video_id → card payload, for unqueue/bulk lookups
  // Catalogues from the server (GET /api/settings) — never duplicated here.
  qualities: [],
  sbOptions: [],
  phases: new Set(),      // phases with no meaningful percent
  quality: "1080p",
  sponsorblock: new Set(["sponsor", "selfpromo"]),
  sbMode: "remove",
  queue: [],
  running: false,
  history: [],
  concurrency: 1,
  maxConcurrency: 4,
  embedMetadata: true,
  embedThumbnail: true,
  embedChapters: true,
  useArchive: false,
  subtitles: false,
  subtitleLangs: "en",
  subtitleAuto: false,
  subtitleEmbed: true,
  cookiesBrowser: "",
  cookiesFile: "",
  cookieBrowsers: [],
  diskHardMin: 500 * 1024 * 1024,
};

const FETCH_LABEL = () => [svgEl(ICONS.search), "Fetch"];

// ---------- settings: quality & SponsorBlock ----------

function renderSponsorBlock() {
  const grid = $("#sb-grid");
  grid.innerHTML = "";
  for (const { id: catId, label } of state.sbOptions) {
    const id = `sb-${catId}`;
    const input = el("input", { type: "checkbox", id });
    input.checked = state.sponsorblock.has(catId);
    input.addEventListener("change", () => {
      if (input.checked) state.sponsorblock.add(catId);
      else state.sponsorblock.delete(catId);
      persistChoices();
    });
    const box = el("span", { class: "box" }, [svgEl(ICONS.check, 12)]);
    grid.appendChild(el("label", { class: "check", for: id }, [input, box, label]));
  }
}

function renderSbMode() {
  $("#sb-mode-seg").querySelectorAll("button").forEach((b) => {
    const on = b.dataset.mode === state.sbMode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
}

function selectQuality(id) {
  state.quality = id;
  renderQuality();
  persistChoices();
  const active = $("#quality-seg button.active");
  if (active) active.focus();
}

function renderQuality() {
  const seg = $("#quality-seg");
  seg.innerHTML = "";
  const qs = state.qualities;
  qs.forEach((q, ix) => {
    const isActive = q.id === state.quality;
    const btn = el("button", {
      type: "button", role: "radio", class: isActive ? "active" : "",
      "aria-checked": isActive ? "true" : "false",
      tabindex: isActive ? "0" : "-1",     // roving tabindex
    }, q.label);
    btn.addEventListener("click", () => selectQuality(q.id));
    btn.addEventListener("keydown", (e) => {
      const delta = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!delta) return;
      e.preventDefault();
      selectQuality(qs[(ix + delta + qs.length) % qs.length].id);
    });
    seg.appendChild(btn);
  });
  const current = qs.find((q) => q.id === state.quality);
  $("#quality-hint").textContent = current ? current.hint : "";
}

function renderMusicFormat() {
  const seg = $("#music-format-seg");
  seg.replaceChildren(...state.musicFormats.map((f) => {
    const on = f.id === state.musicFormat;
    return el("button", { type: "button", role: "radio", class: on ? "active" : "",
      "aria-checked": on ? "true" : "false",
      onclick: () => { state.musicFormat = f.id; renderMusicFormat(); saveSettings({ music_format: f.id }); } },
    f.label);
  }));
  const cur = state.musicFormats.find((f) => f.id === state.musicFormat);
  $("#music-format-hint").textContent = cur ? cur.hint : "";
  $("#music-keep-full").checked = state.musicKeepFull;
  const sel = $("#sync-hours");
  if (sel.options.length !== state.syncChoices.length) {
    sel.replaceChildren(...state.syncChoices.map((h) =>
      el("option", { value: String(h) }, h === 0 ? "Off — only when I ask" : `Every ${plural(h, "hour")}`)));
  }
  sel.value = String(state.syncHours);
}

function qualityLabel(id) {
  const q = state.qualities.find((x) => x.id === id);
  return q ? q.label : id;
}

let persistTimer = null;
function persistChoices() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    post("/api/settings", {
      last_quality: state.quality,
      last_sponsorblock: Array.from(state.sponsorblock),
      last_sb_mode: state.sbMode,
    }).catch(() => {});
  }, 300);
}

async function saveSettings(changes, okMessage) {
  try {
    await post("/api/settings", changes);
    if (okMessage) toast(okMessage);
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------- settings: other panels ----------

function renderHistory() {
  const dl = $("#channel-history");
  dl.innerHTML = "";
  for (const url of state.history) dl.appendChild(el("option", { value: url }));
}

function renderAdvancedPanel() {
  $("#embed-metadata").checked = state.embedMetadata;
  $("#embed-thumbnail").checked = state.embedThumbnail;
  $("#embed-chapters").checked = state.embedChapters;
  $("#use-archive").checked = state.useArchive;
  const n = $("#concurrency-input");
  n.max = String(state.maxConcurrency);
  n.value = String(state.concurrency);
  const sel = $("#cookies-browser");
  const existing = new Set(Array.from(sel.options).map((o) => o.value));
  for (const b of state.cookieBrowsers) {
    if (!existing.has(b)) sel.appendChild(el("option", { value: b }, b.charAt(0).toUpperCase() + b.slice(1)));
  }
  sel.value = state.cookiesBrowser || "";
  const file = $("#cookies-file");
  if (document.activeElement !== file) file.value = state.cookiesFile || "";
}

function renderSubtitlesPanel() {
  $("#subs-enabled").checked = state.subtitles;
  const langs = $("#subs-langs");
  if (document.activeElement !== langs) langs.value = state.subtitleLangs || "en";
  $("#subs-auto").checked = state.subtitleAuto;
  $("#subs-embed").checked = state.subtitleEmbed;
}

function setDestination(dir) {
  $("#dest-path").textContent = dir;
  const input = $("#dest-input");
  if (document.activeElement !== input) input.value = dir;
}

// Server settings fields → state keys (shared by the initial load and the
// live "settings" events, so another tab's changes land the same way).
const SETTING_KEYS = {
  concurrency: "concurrency", embed_metadata: "embedMetadata", embed_thumbnail: "embedThumbnail",
  embed_chapters: "embedChapters", use_archive: "useArchive", subtitles: "subtitles",
  subtitle_langs: "subtitleLangs", subtitle_auto: "subtitleAuto", subtitle_embed: "subtitleEmbed",
  cookies_browser: "cookiesBrowser", cookies_file: "cookiesFile", history: "history",
  music_format: "musicFormat", music_keep_full: "musicKeepFull", podcast_sync_hours: "syncHours",
  podcast_follows: "follows",
};

function applySettingFields(s) {
  for (const [field, key] of Object.entries(SETTING_KEYS)) {
    if (field in s && s[field] != null) state[key] = s[field];
  }
  if (s.download_dir) setDestination(s.download_dir);
  if ("history" in s) renderHistory();
  renderAdvancedPanel();
  renderSubtitlesPanel();
  renderMusicFormat();
  if ("podcast_follows" in s) renderFollows();
}

function applySettings(s) {
  if (Array.isArray(s.qualities)) state.qualities = s.qualities;
  if (Array.isArray(s.sponsorblock)) state.sbOptions = s.sponsorblock;
  if (Array.isArray(s.indeterminate_phases)) state.phases = new Set(s.indeterminate_phases);
  if (typeof s.max_concurrency === "number") state.maxConcurrency = s.max_concurrency;
  if (Array.isArray(s.cookie_browsers)) state.cookieBrowsers = s.cookie_browsers;
  if (Array.isArray(s.music_formats)) state.musicFormats = s.music_formats;
  if (Array.isArray(s.podcast_sync_choices)) state.syncChoices = s.podcast_sync_choices;
  if (typeof s.last_quality === "string") state.quality = s.last_quality;
  if (Array.isArray(s.last_sponsorblock)) state.sponsorblock = new Set(s.last_sponsorblock);
  if (typeof s.last_sb_mode === "string") state.sbMode = s.last_sb_mode;
  applySettingFields(s);
  renderSponsorBlock();
  renderSbMode();
  renderQuality();
  renderQueue();          // phases/labels may have changed the rows
}

// ---------- health: engine & disk ----------

async function checkHealth() {
  let h;
  try { h = await api("/api/health"); } catch (err) { return; }
  const warn = [];
  if (!h.yt_dlp) warn.push("yt-dlp not on PATH");
  if (!h.ffmpeg) warn.push("ffmpeg not on PATH");
  const hint = $("#env-hint");
  hint.textContent = warn.length
    ? `Missing: ${warn.join(" · ")}. Install before queueing downloads.`
    : "yt-dlp and ffmpeg detected.";
  hint.classList.toggle("is-error", warn.length > 0);
  if (h.version) $("#app-version").textContent = "v" + h.version;
  if (h.disk_hard_min) state.diskHardMin = h.disk_hard_min;
  $("#ytdlp-ver").textContent = h.yt_dlp
    ? `${h.yt_dlp_version || "ok"}${h.yt_dlp_source === "binary" ? " · binary" : ""}` : "missing";
  $("#ffmpeg-ver").textContent = h.ffmpeg ? (h.ffmpeg_version || "ok") : "missing";
  $("#update-ytdlp").disabled = !h.yt_dlp;
  const note = $("#ytdlp-note");
  note.hidden = h.yt_dlp_source !== "binary";
  note.textContent = "Using the standalone yt-dlp binary — progress can stall on Windows. Run "
    + "`pip install yt-dlp` in the server's Python so Chive can drive yt-dlp directly for live progress.";
  const read = $("#disk-readout");
  read.textContent = h.disk_free != null
    ? `${fmtBytes(h.disk_free)} free${h.disk_total ? " / " + fmtBytes(h.disk_total) : ""}` : "";
  read.classList.toggle("low", !!h.disk_low);
  const banner = $("#disk-banner");
  banner.hidden = !(h.disk_low && h.disk_free != null);
  if (!banner.hidden) {
    banner.textContent = `Low disk space — only ${fmtBytes(h.disk_free)} free at the destination. `
      + `Downloads stop below ${fmtBytes(state.diskHardMin)}.`;
  }
}

async function updateYtdlp() {
  const btn = $("#update-ytdlp");
  if (btn.disabled) return;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "Updating…";
  try {
    const r = await post("/api/tools/update-ytdlp");
    toast(r.ok ? `yt-dlp: ${r.version || "updated"}` : (r.hint || "yt-dlp update reported an issue — see log."),
      r.ok ? "" : "notice");
    if (r.output) showStaticLog("yt-dlp update", (r.hint ? r.hint + "\n\n" : "") + r.output);
    checkHealth();
  } catch (err) {
    toast(err.message || "Update failed.", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ---------- source: YouTube ⇄ podcast mode ----------

function setMode(mode) {
  state.mode = MODES[mode] ? mode : "youtube";
  state.podcast = null;
  state.videos = [];
  state.allVideos = [];
  state.page = 1;
  state.filter = "";
  const m = MODES[state.mode];
  document.documentElement.dataset.mode = state.mode;
  $("#mode-seg").querySelectorAll("button").forEach((b) => {
    const on = b.dataset.mode === state.mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
  const input = $("#channel-url");
  input.value = "";
  input.placeholder = m.placeholder;
  const gf = $("#grid-filter");
  gf.value = "";
  gf.placeholder = m.filter;
  $("#select-page").title = m.queueAll;
  $("#src-title").textContent = m.title;
  $("#scrape-hint").textContent = m.hint;
  hideResults();
  $("#channel-meta").hidden = true;
  $("#grid-section").hidden = true;
}

const submitSource = () => ({ podcast: podcastSearch, music: resolveMusic, web: resolveWeb }[state.mode] || scrape)();
const loadCurrent = () => ({ podcast: loadEpisodes, music: pageResolved, web: pageResolved }[state.mode] || loadPage)();

function hideResults() {
  const box = $("#channel-results");
  box.hidden = true;
  box.innerHTML = "";
}

function resultsNote(text) {
  const box = $("#channel-results");
  box.hidden = false;
  box.innerHTML = "";
  box.appendChild(el("p", { class: "muted channel-results-note" }, text));
  return box;
}

function setBusy(busy, label = "Fetching…") {
  const btn = $("#scrape-btn");
  btn.disabled = busy;
  btn.replaceChildren(...(busy ? [label] : FETCH_LABEL()));
}

// ---------- YouTube ----------

function filterPayload() {
  const f = state.filters;
  return {
    query: state.filter.trim() || undefined, sort: state.sort,
    duration_min: f.durationMin || undefined, duration_max: f.durationMax || undefined,
    views_min: f.viewsMin || undefined, views_max: f.viewsMax || undefined,
    date_from: f.dateFrom || undefined, date_to: f.dateTo || undefined,
  };
}

async function scrape() {
  const raw = $("#channel-url").value.trim();
  if (!raw) { toast("Enter a channel URL, @handle, or a name to search.", "notice"); return; }
  state.channelUrl = raw;
  state.page = 1;
  state.filter = "";
  $("#grid-filter").value = "";
  hideResults();
  await loadPage();
}

async function loadPage() {
  setBusy(true);
  const hint = $("#scrape-hint");
  const prevHint = hint.textContent;
  if (state.page > 1 || state.filtered) {
    hint.textContent = "Scanning the channel… filtering the whole channel can take a moment.";
  }
  try {
    const data = await post("/api/scrape", {
      url: state.channelUrl, page: state.page, ignore_shorts: state.ignoreShorts, ...filterPayload(),
    });
    if (data.needs_search) { await showChannelSearch(data.query); return; }
    if (data.resolved_url) state.channelUrl = data.resolved_url;
    if (typeof data.page === "number") state.page = data.page;   // the server may clamp it
    showVideos(data, !!data.filtered);
    const note = $("#shorts-skip-note");
    note.textContent = state.ignoreShorts && data.skipped_shorts > 0
      ? `· ${plural(data.skipped_shorts, "short")} hidden ${data.filtered ? "in this channel" : "on this page"}`
      : "";
  } catch (err) {
    toast(err.message || String(err), "error");
  } finally {
    setBusy(false);
    hint.textContent = prevHint;
  }
}

async function showChannelSearch(query) {
  const box = resultsNote(`Searching channels for “${query}”…`);
  try {
    const { channels = [] } = await post("/api/search", { query });
    if (!channels.length) {
      resultsNote("No channels found. Try a more specific name or paste the channel URL.");
      return;
    }
    resultsNote("Pick a channel:");
    for (const c of channels) {
      box.appendChild(el("button", {
        type: "button", class: "channel-result",
        onclick: () => {
          state.channelUrl = c.url;
          $("#channel-url").value = c.url;
          hideResults();
          state.page = 1;
          loadPage();
        },
      }, [el("span", { class: "channel-result-name" }, c.name),
        el("span", { class: "channel-result-url mono" }, c.url)]));
    }
  } catch (err) {
    resultsNote(err.message || "Search failed.");
  }
}

// ---------- podcasts ----------

async function podcastSearch() {
  const q = $("#channel-url").value.trim();
  if (!q) { toast("Type a podcast name to search.", "notice"); return; }
  state.podcast = null;
  $("#grid-section").hidden = true;
  $("#channel-meta").hidden = true;
  const box = resultsNote(`Searching podcasts for “${q}”…`);
  setBusy(true, "Searching…");
  try {
    const { shows = [] } = await post("/api/podcast/search", { query: q });
    if (!shows.length) { resultsNote("No podcasts found. Try a different name."); return; }
    resultsNote("Pick a podcast:");
    for (const s of shows) {
      const art = s.artwork
        ? el("img", { class: "pod-art", src: s.artwork, alt: "", loading: "lazy" })
        : el("span", { class: "pod-art pod-art-blank" });
      box.appendChild(el("button", {
        type: "button", class: "channel-result podcast-result", onclick: () => openPodcast(s),
      }, [art, el("span", { class: "pod-meta" }, [
        el("span", { class: "channel-result-name" }, s.name),
        el("span", { class: "channel-result-url" },
          [s.author, s.episode_count ? `· ${plural(s.episode_count, "episode")}` : ""].filter(Boolean).join(" ")),
      ])]));
    }
  } catch (err) {
    resultsNote(err.message || "Search failed.");
  } finally {
    setBusy(false);
  }
}

// ---------- podcast follows ----------

function renderFollows() {
  const list = $("#follow-list");
  $("#follow-strip").classList.toggle("is-empty", !state.follows.length);
  if (!state.follows.length) {
    list.replaceChildren(el("p", { class: "muted follow-empty" },
      "Follow a show to keep it archived — new episodes are queued when Chive checks for them."));
  } else {
    list.replaceChildren(...state.follows.map((f) => el("span", { class: "follow-chip" }, [
      el("button", { type: "button", class: "follow-open", title: `Open ${f.name}`,
        onclick: () => openPodcast(f) }, [
        f.artwork ? el("img", { src: f.artwork, alt: "", loading: "lazy" }) : null, f.name]),
      el("button", { type: "button", class: "follow-x", title: `Unfollow ${f.name}`, "aria-label": `Unfollow ${f.name}`,
        onclick: () => setFollow(f, false) }, svgEl(ICONS.close, 11)),
    ])));
  }
  const btn = $("#follow-btn");
  const on = !!state.podcast && state.follows.some((f) => f.feed_url === state.podcast.feed_url);
  btn.textContent = on ? "Following ✓" : "Follow";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.classList.toggle("active", on);
}

async function setFollow(show, on) {
  try {
    const r = await post(on ? "/api/podcast/follow" : "/api/podcast/unfollow",
      { feed_url: show.feed_url, name: show.name, artwork: show.artwork });
    state.follows = r.follows;
    renderFollows();
    toast(on ? `Following ${show.name} — new episodes will be queued when Chive checks.` : `Unfollowed ${show.name}.`);
  } catch (err) { toast(err.message, "error"); }
}

async function syncFollows(feedUrl) {
  const btn = $("#sync-all");
  btn.disabled = true;
  try {
    const r = await post("/api/podcast/sync", feedUrl ? { feed_url: feedUrl } : {});
    const errors = Object.keys(r.errors || {});
    toast(r.added ? `Queued ${plural(r.added, "new episode")}.` : "No new episodes.", r.added ? "" : "notice");
    if (errors.length) toast(`Couldn't check: ${errors.join(", ")}`, "error");
  } catch (err) { toast(err.message, "error"); }
  finally { btn.disabled = false; }
}

async function importOpml(file) {
  try {
    const r = await post("/api/podcast/opml", { opml: await file.text() });
    state.follows = r.follows;
    renderFollows();
    toast(`Imported — now following ${plural(r.added, "new show")}.`);
  } catch (err) { toast(err.message, "error"); }
}

function openPodcast(show) {
  state.podcast = { name: show.name, feed_url: show.feed_url, artwork: show.artwork };
  state.page = 1;
  state.filter = "";
  $("#grid-filter").value = "";
  hideResults();
  loadEpisodes();
}

async function loadEpisodes() {
  if (!state.podcast) return;
  const hint = $("#scrape-hint");
  const prevHint = hint.textContent;
  hint.textContent = `Loading “${state.podcast.name}”…`;
  setBusy(true, "Loading…");
  try {
    const data = await post("/api/podcast/episodes", {
      feed_url: state.podcast.feed_url, page: state.page, query: state.filter.trim() || undefined,
    });
    if (typeof data.page === "number") state.page = data.page;
    if (data.artwork) state.podcast.artwork = state.podcast.artwork || data.artwork;
    showVideos(data, true);
    renderFollows();
    $("#shorts-skip-note").textContent = "";
    hint.textContent = `Browsing “${state.podcast.name}” — click an episode to queue it, or use Queue all.`;
  } catch (err) {
    hint.textContent = prevHint;
    toast(err.message || "Could not load episodes.", "error");
  } finally {
    setBusy(false);
  }
}

async function queueWholePodcast() {
  if (!state.podcast) return;
  try {
    const data = await post("/api/podcast/episodes", { feed_url: state.podcast.feed_url, page: 1, page_size: 5000 });
    const eps = (data.videos || []).filter((v) => !v.downloaded && !state.queuedIds.has(v.video_id));
    if (!eps.length) { toast("Every episode is already downloaded or queued.", "notice"); return; }
    await queueVideos(eps, "episode");
  } catch (err) { toast(err.message, "error"); }
}

// ---------- music & any-site (resolved lists, paged locally) ----------

async function resolveList(endpoint, body, label) {
  hideResults();
  setBusy(true, label);
  try {
    const data = await post(endpoint, body);
    state.allVideos = data.videos || [];
    state.page = 1;
    state.filter = "";
    $("#grid-filter").value = "";
    pageResolved();
    const hint = $("#scrape-hint");
    if (data.kind === "playlist" && (data.album || data.title)) {
      hint.textContent = `${data.album || data.title}${data.artist ? " — " + data.artist : ""} · `
        + `${plural(state.allVideos.length, "item")}. Click to queue, or Queue all.`;
    } else {
      hint.textContent = MODES[state.mode].hint;
    }
  } catch (err) {
    toast(err.message || String(err), "error");
  } finally {
    setBusy(false);
  }
}

function resolveMusic() {
  const input = $("#channel-url").value.trim();
  if (!input) { toast("Paste an album URL, or type an album to search.", "notice"); return; }
  return resolveList("/api/music/resolve", { input }, input.includes("://") ? "Looking up…" : "Searching…");
}

function resolveWeb() {
  const url = $("#channel-url").value.trim();
  if (!url) { toast("Paste a link to a video or playlist.", "notice"); return; }
  return resolveList("/api/web/resolve", { url }, "Looking up…");
}

function pageResolved() {
  const q = state.filter.trim().toLowerCase();
  const matches = q ? state.allVideos.filter((v) =>
    `${v.title} ${v.artist || ""} ${v.album || ""} ${v.channel || ""}`.toLowerCase().includes(q)) : state.allVideos;
  const totalPages = Math.max(1, Math.ceil(matches.length / 50));
  state.page = Math.min(Math.max(1, state.page), totalPages);
  const videos = matches.slice((state.page - 1) * 50, state.page * 50);
  showVideos({ videos, page: state.page, total_pages: totalPages, count: videos.length, total: matches.length,
    filtered: true }, true);
  $("#shorts-skip-note").textContent = "";
}

// ---------- the grid ----------

function showVideos(data, filtered) {
  state.videos = data.videos || [];
  for (const v of state.videos) state.videoById.set(v.video_id, v);
  state.filtered = filtered;
  renderMeta(data);
  renderGrid();
  $("#channel-meta").hidden = false;
  $("#grid-section").hidden = false;
}

function renderMeta(data) {
  $("#meta-page").textContent = data.total_pages ? `${data.page} / ${data.total_pages}` : String(data.page);
  $("#meta-count").textContent = String(data.count || 0);
  updateSelectedCount();
  $("#prev-page").disabled = state.page <= 1;
  if (state.filtered) {
    $("#next-page").disabled = state.page >= (data.total_pages || 1);
  } else {
    // Page off the raw window, not the post-Shorts count: a page of hidden
    // Shorts still has more pages behind it.
    const windowSize = data.page_entries != null ? data.page_entries : (data.count || 0);
    $("#next-page").disabled = windowSize < (data.page_size || 50);
  }
  const count = $("#filter-count");
  if (state.mode === "podcast" && data.total != null) count.textContent = plural(data.total, "episode");
  else if (data.filtered) {
    count.textContent = plural(data.total, "match", "matches") + (data.capped ? " (first 2000)" : "");
  } else count.textContent = "";
}

function renderGrid() {
  const grid = $("#video-grid");
  grid.innerHTML = "";
  const empty = $("#grid-empty");
  if (!state.videos.length) {
    empty.hidden = false;
    empty.querySelector("p").textContent = state.mode === "podcast" ? "No episodes match."
      : (state.filter.trim() || state.filtered ? "No matches in this channel." : "No videos on this page.");
    return;
  }
  empty.hidden = true;
  for (const v of state.videos) grid.appendChild(renderCard(v));
  updateSelectedCount();
}

function renderCard(v) {
  const queued = state.queuedIds.has(v.video_id);
  const downloaded = !!v.downloaded && !queued;      // on disk and not in the queue → locked
  const title = v.title || v.video_id;
  const card = el("article", {
    class: `video-card${queued ? " selected" : ""}${downloaded ? " downloaded" : ""}`,
    "data-id": v.video_id, role: "button",
    tabindex: downloaded ? "-1" : "0",
    "aria-pressed": queued ? "true" : "false",
    "aria-disabled": downloaded ? "true" : "false",
    "aria-label": downloaded ? `Already downloaded: ${title}` : `${queued ? "Queued: " : "Queue "}${title}`,
  });
  if (!downloaded) {
    card.addEventListener("click", () => toggleCard(v.video_id));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleCard(v.video_id); }
    });
  }
  card.appendChild(el("div", { class: "thumb-wrap" }, [
    v.thumbnail ? el("img", { src: v.thumbnail, alt: "", loading: "lazy" }) : null,
    v.duration ? el("span", { class: "thumb-badge" }, fmtDuration(v.duration)) : null,
    el("span", { class: "thumb-mark" }, [svgEl(ICONS.check)]),
    downloaded ? el("span", { class: "dl-badge" }, [svgEl(ICONS.check, 12), "In library"]) : null,
  ]));
  const meta = [cardDetail(v),
    downloaded ? "· downloaded" : (queued ? "· queued" : (v.view_count ? `· ${fmtViews(v.view_count)} views` : "")),
  ].filter(Boolean).join(" ");
  card.appendChild(el("div", { class: "card-body" }, [
    el("h3", { class: "card-title" }, v.title), el("div", { class: "card-meta" }, meta),
  ]));
  return card;
}

function cardDetail(v) {
  if (state.mode === "music") {
    const what = v.music_mode === "track"
      ? (v.track ? `Track ${v.track}` : "Track")
      : (v.chapters ? `Album · ${plural(v.chapters, "track")}` : "Album");
    return [what, v.artist].filter(Boolean).join(" · ");
  }
  if (state.mode === "web" && v.site) return [v.site, v.channel].filter(Boolean).join(" · ");
  return v.upload_date ? fmtDate(v.upload_date) : "";
}

function markCard(videoId, on) {
  const card = byDataId(".video-card", videoId);
  if (!card) return;
  card.classList.toggle("selected", on);
  card.setAttribute("aria-pressed", on ? "true" : "false");
}

function updateSelectedCount() {
  $("#meta-selected").textContent = String(state.videos.filter((v) => state.queuedIds.has(v.video_id)).length);
}

const EXTRA_FIELDS = {
  podcast: ["collection", "author", "artwork", "upload_date"],
  music: ["artist", "album", "album_artist", "year", "track", "track_total", "music_mode"],
};

function videoPayload(v) {
  const p = { video_id: v.video_id, url: v.url, title: v.title, thumbnail: v.thumbnail, duration: v.duration };
  for (const k of EXTRA_FIELDS[state.mode] || []) if (v[k] != null) p[k] = v[k];
  return p;
}

/** Queue videos with the current choices (optimistically marked). */
async function queueVideos(videos, noun = "item") {
  for (const v of videos) { state.queuedIds.add(v.video_id); markCard(v.video_id, true); }
  updateSelectedCount();
  const r = await post("/api/queue/add", {
    videos: videos.map(videoPayload), source: state.mode, quality: state.quality,
    sponsorblock: Array.from(state.sponsorblock), sponsorblock_mode: state.sbMode,
  });
  if (videos.length > 1) toast(`Queued ${plural(r.count || 0, noun)}.`);
  return r;
}

// Clicking a card queues it; clicking a queued (still waiting) card removes
// it. Optimistic UI, reconciled by the SSE events.
async function toggleCard(videoId) {
  const v = state.videoById.get(videoId);
  if (!v) return;
  if (!state.queuedIds.has(videoId)) {
    try { await queueVideos([v]); } catch (err) { toast(err.message, "error"); }
    return;
  }
  const qi = state.queue.find((q) => q.video_id === videoId && (q.status === "pending" || q.status === "paused"));
  if (!qi) { toast("Already downloading or finished — can't unqueue.", "notice"); return; }
  state.queuedIds.delete(videoId);
  markCard(videoId, false);
  updateSelectedCount();
  try { await post("/api/queue/remove", { id: qi.id }); } catch (err) { toast(err.message, "error"); }
}

async function selectPage() {
  if (state.mode === "podcast") return queueWholePodcast();
  // Music/any-site lists are fully known: queue all of them, not just this page.
  const pool = state.mode === "music" || state.mode === "web" ? state.allVideos : state.videos;
  const toAdd = pool.filter((v) => !state.queuedIds.has(v.video_id) && !v.downloaded);
  if (!toAdd.length) { toast("Nothing new to queue on this page.", "notice"); return; }
  try { await queueVideos(toAdd); } catch (err) { toast(err.message, "error"); }
}

async function clearPageSelection() {
  const onPage = new Set(state.videos.map((v) => v.video_id));
  const removable = state.queue.filter((q) => (q.status === "pending" || q.status === "paused") && onPage.has(q.video_id));
  if (!removable.length) { toast("Nothing queued on this page to remove.", "notice"); return; }
  for (const q of removable) { state.queuedIds.delete(q.video_id); markCard(q.video_id, false); }
  updateSelectedCount();
  await Promise.allSettled(removable.map((q) => post("/api/queue/remove", { id: q.id })));
}

// ---------- the queue ----------

const EMPTY_LINES = [
  "Nothing queued yet. Click a thumbnail above to plant one.",
  "An empty plot. Click a thumbnail above to start archiving.",
  "Quiet in here. Pick a video above and it lands in the queue.",
  "Nothing growing yet — click a thumbnail to queue it.",
];

function freshEmptyLine() {
  $("#queue-empty").textContent = EMPTY_LINES[(Math.random() * EMPTY_LINES.length) | 0];
}

function renderQueueHeader() {
  $("#q-count").textContent = String(state.queue.length);
  $("#q-meta").textContent = state.queue.length === 1 ? "item" : "items";
  state.queuedIds = queuedVideoIds(state.queue);
  renderQueueStats();
  const empty = $("#queue-empty");
  if (!state.queue.length && empty.hidden) freshEmptyLine();
  empty.hidden = state.queue.length > 0;
}

function renderQueue() {
  const list = $("#queue-list");
  list.querySelectorAll(".qitem").forEach((n) => n.remove());
  for (const it of state.queue) list.appendChild(renderQItem(it));
  renderQueueHeader();
}

function renderQueueStats() {
  const bar = $("#queue-stats");
  bar.hidden = !state.queue.length;
  if (bar.hidden) return;
  const s = queueStats(state.queue);
  $("#stat-remaining").textContent = String(s.remaining);
  $("#stat-done").textContent = String(s.done);
  $("#stat-speed").textContent = s.speed > 0 ? `${fmtBytes(s.speed)}/s` : "—";
  $("#stat-size").textContent = fmtBytes(s.archived);
  $("#stat-eta").textContent = s.eta != null ? fmtClock(s.eta) : "—";
}

function sourceChip(it) {
  if (it.source === "podcast") return el("span", { class: "qchip qchip-pod" }, "Podcast");
  if (it.source === "music") {
    const mode = it.extra && it.extra.music_mode === "track" ? "Track" : "Album";
    return el("span", { class: "qchip qchip-music" }, `${mode} · ${(it.quality || "").toUpperCase()}`);
  }
  if (it.source === "web") return el("span", { class: "qchip qchip-web" }, `Web · ${qualityLabel(it.quality)}`);
  return el("span", { class: "qchip" }, qualityLabel(it.quality));
}

const STATUS_ICONS = {
  downloading: ICONS.download, completed: ICONS.check, failed: ICONS.alert, cancelled: ICONS.close,
};

function actionButton(title, icon, onclick, extra = {}) {
  return el("button", { type: "button", title, "aria-label": title, onclick, ...extra }, svgEl(icon));
}

function renderQItem(it) {
  const node = el("div", { class: `qitem ${it.status}`, "data-id": it.id, "data-status": it.status });
  node.appendChild(el("div", { class: "qthumb" },
    it.thumbnail ? el("img", { src: it.thumbnail, alt: "", loading: "lazy" }) : null));

  const meta = el("div", { class: "qmeta" }, [
    el("span", { class: `qstatus ${it.status}` }, [
      svgEl(STATUS_ICONS[it.status] || ICONS.clock, 12),
      el("em", { class: "status-verb" }, statusText(it)),
    ]),
    sourceChip(it),
  ]);
  if (it.source !== "podcast" && it.status === "pending" && it.sponsorblock && it.sponsorblock.length) {
    meta.appendChild(el("span", { class: "qchip", title: it.sponsorblock.join(", ") },
      `SponsorBlock · ${plural(it.sponsorblock.length, "category", "categories")}`));
  }
  if (it.status === "downloading") {
    meta.appendChild(el("span", { class: "qchip mono qchip-speed", hidden: !it.speed }, it.speed || ""));
    meta.appendChild(el("span", { class: "qchip mono qchip-eta", hidden: !it.eta }, it.eta ? `eta ${it.eta}` : ""));
    meta.appendChild(el("span", { class: "qchip mono qchip-pct" }, `${(it.progress || 0).toFixed(1)}%`));
  }
  if (it.status === "paused" && (it.progress || 0) > 0) {
    meta.appendChild(el("span", { class: "qchip mono" }, `${it.progress.toFixed(1)}% kept`));
  }
  const bar = el("div", { class: "qprogress" }, el("div", { class: "bar" }));
  node.appendChild(el("div", { class: "qbody" }, [el("h3", { class: "qtitle" }, it.title || it.video_id), meta, bar]));
  setBar(bar, it);

  const title = it.title || it.video_id;
  const actions = el("div", { class: "qactions" });
  const add = (cond, ...args) => { if (cond) actions.appendChild(actionButton(...args)); };
  add(it.status === "downloading" || it.status === "pending", "Pause", ICONS.pause, () => itemAction("pause-item", it.id));
  add(it.status === "paused", "Resume", ICONS.play, () => itemAction("resume-item", it.id));
  add(it.status === "downloading", "Cancel download", ICONS.stop, () => itemAction("cancel", it.id), { class: "danger" });
  add(it.status === "failed" || it.status === "cancelled", "Retry", ICONS.retry, () => itemAction("retry", it.id));
  add(it.status === "completed", "Reveal in folder", ICONS.folder, () => itemAction("reveal", it.id));
  add(it.status !== "pending", "View log", ICONS.log, () => openLog(it.id, title));
  add(true, "Remove", ICONS.close, () => itemAction("remove", it.id), { disabled: it.status === "downloading" });
  node.appendChild(actions);
  return node;
}

function setBar(bar, it) {
  const indeterminate = isIndeterminate(it, state.phases);
  bar.classList.toggle("indeterminate", indeterminate);
  bar.firstChild.style.width = indeterminate ? "100%" : `${Math.max(0, Math.min(100, it.progress || 0))}%`;
}

/** Update a row in place (the hot path for progress), or rebuild it when
 *  its status changed, since that restructures its chips and buttons. */
function patchQItem(it, statusChanged) {
  const node = byDataId(".qitem", it.id);
  if (!node) { renderQueue(); return; }
  if (statusChanged || node.dataset.status !== it.status) { node.replaceWith(renderQItem(it)); return; }
  setBar(node.querySelector(".qprogress"), it);
  node.querySelector(".status-verb").textContent = statusText(it);
  const speed = node.querySelector(".qchip-speed");
  const eta = node.querySelector(".qchip-eta");
  const pct = node.querySelector(".qchip-pct");
  if (speed) { speed.textContent = it.speed || ""; speed.hidden = !it.speed; }
  if (eta) { eta.textContent = it.eta ? `eta ${it.eta}` : ""; eta.hidden = !it.eta; }
  if (pct) pct.textContent = `${(it.progress || 0).toFixed(1)}%`;
}

async function itemAction(action, id) {
  try { await post(`/api/queue/${action}`, { id }); } catch (err) { toast(err.message, "error"); }
}

async function clearFinished() {
  const finished = state.queue.filter((i) => ["completed", "failed", "cancelled"].includes(i.status)).length;
  if (!finished) { toast("Nothing finished to clear.", "notice"); return; }
  if (!confirm(`Remove ${plural(finished, "finished item")} from the queue?`)) return;
  try { await post("/api/queue/clear"); } catch (err) { toast(err.message, "error"); }
}

function setRunning(running) {
  state.running = running;
  $("#q-start").hidden = running;
  $("#q-pause").hidden = !running;
}

// ---------- live events (SSE) ----------

function setConn(name) {
  const pill = $("#conn-pill");
  pill.dataset.state = name;
  pill.querySelector(".conn-label").textContent =
    name === "live" ? "live" : name === "reconnecting" ? "reconnecting…" : "connecting…";
}

function connectEvents() {
  setConn("connecting");
  const src = new EventSource("/api/events");
  src.onopen = () => setConn("live");
  src.onmessage = (ev) => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch { return; }
    setConn("live");
    handleEvent(payload);
  };
  // EventSource reconnects by itself; show the gap meanwhile.
  src.onerror = () => { if (src.readyState !== EventSource.OPEN) setConn("reconnecting"); };
}

function announce(it) {
  const title = it.title || it.video_id;
  if (it.status === "completed") notify("Downloaded", title, it.id);
  else if (it.status === "failed") notify("Download failed", `${title} — ${it.message || "see its log"}`, it.id);
  const { remaining } = queueStats(state.queue);
  if (state.remainingBefore > 0 && remaining === 0) {
    const { done, failed } = queueStats(state.queue);
    notify("Queue finished", `${plural(done, "download")} complete${failed ? `, ${failed} failed` : ""}.`, "queue");
  }
}

function handleEvent(payload) {
  state.remainingBefore = queueStats(state.queue).remaining;
  const r = applyQueueEvent(state.queue, payload);
  state.queue = r.queue;
  updateTitle(state.queue);
  if (r.redraw === "all") {
    renderQueue();
    renderGrid();
  } else if (r.redraw === "item") {
    patchQItem(r.item, r.statusChanged);
    renderQueueStats();
    if (r.statusChanged) {
      state.queuedIds = queuedVideoIds(state.queue);
      markCard(r.item.video_id, state.queuedIds.has(r.item.video_id));
      updateSelectedCount();
      announce(r.item);
    }
  } else if (r.redraw === "removed") {
    for (const id of r.ids) { const row = byDataId(".qitem", id); if (row) row.remove(); }
    renderQueueHeader();
    for (const v of state.videos) markCard(v.video_id, state.queuedIds.has(v.video_id));
    updateSelectedCount();
  }
  switch (payload.type) {
    case "snapshot":
      if (typeof payload.running === "boolean") setRunning(payload.running);
      break;
    case "state":
      // The authoritative running flag. (Never inferred from progress: an
      // in-flight download keeps reporting after Hold is clicked.)
      setRunning(!!payload.running);
      break;
    case "settings":
      applySettingFields(payload);
      break;
    case "library":
      onLibraryChanged();
      break;
    case "notice":
      toast(payload.message);
      notify("Chive", payload.message, "notice");
      break;
    case "disk":
      if (payload.low) {
        toast("Low disk space at the destination — downloads may stop.", "error");
        checkHealth();
      }
      break;
  }
}

// ---------- views: Archive ⇄ Library ----------

function setView(view, { focus = false } = {}) {
  const library = view === "library";
  $("#view-archive").hidden = library;
  $("#view-library").hidden = !library;
  for (const tab of document.querySelectorAll(".view-tabs [role=tab]")) {
    const on = tab.dataset.view === view;
    tab.classList.toggle("active", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    if (on && focus) tab.focus();
  }
  if (library) showLibrary();
  const hash = library ? "#library" : "";
  if (location.hash !== hash) history.replaceState(null, "", hash || location.pathname);
}

function bindViews() {
  const tabs = Array.from(document.querySelectorAll(".view-tabs [role=tab]"));
  tabs.forEach((tab, ix) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
    tab.addEventListener("keydown", (e) => {
      const delta = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!delta) return;
      e.preventDefault();
      setView(tabs[(ix + delta + tabs.length) % tabs.length].dataset.view, { focus: true });
    });
  });
  window.addEventListener("hashchange", () => setView(location.hash === "#library" ? "library" : "archive"));
  // "/" jumps to the search box of the current view (unless you're typing).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target && e.target.tagName) || "";
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || e.target.isContentEditable) return;
    if (document.querySelector(".modal-overlay:not([hidden])")) return;
    e.preventDefault();
    if ($("#view-library").hidden) $("#channel-url").focus();
    else focusLibrarySearch();
  });
  setView(location.hash === "#library" ? "library" : "archive");
}

// ---------- theme (Dusk ⇄ Dawn) ----------

const THEME_COLORS = { dark: "#0f0d0b", light: "#efe6d8" };

function applyTheme(mode) {
  const light = mode === "light";
  if (light) document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  document.querySelector('meta[name="theme-color"]').setAttribute("content", light ? THEME_COLORS.light : THEME_COLORS.dark);
  const tog = $("#theme-toggle");
  tog.setAttribute("aria-checked", light ? "true" : "false");
  tog.setAttribute("aria-label", light ? "Switch to Dusk (dark) theme" : "Switch to Dawn (light) theme");
}

function initTheme() {
  // The inline <head> script set data-theme before paint; this syncs the
  // toggle's aria state and the theme-color meta.
  let stored = null;
  try { stored = localStorage.getItem("theme"); } catch (e) { /* private mode */ }
  const prefersLight = window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches;
  applyTheme(stored || (prefersLight ? "light" : "dark"));
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
  });
}

// ---------- bindings ----------

function bindSource() {
  $("#scrape-form").addEventListener("submit", (e) => { e.preventDefault(); submitSource(); });
  $("#mode-seg").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("#ignore-shorts").addEventListener("change", (e) => {
    state.ignoreShorts = e.target.checked;
    if (state.channelUrl) loadPage();
  });
  $("#prev-page").addEventListener("click", () => { if (state.page > 1) { state.page--; loadCurrent(); } });
  $("#next-page").addEventListener("click", () => { state.page++; loadCurrent(); });
  $("#select-page").addEventListener("click", selectPage);
  $("#clear-selection").addEventListener("click", clearPageSelection);

  // Title search (debounced) — whole channel for YouTube, the feed for podcasts.
  let filterTimer = null;
  $("#grid-filter").addEventListener("input", (e) => {
    state.filter = e.target.value;
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { state.page = 1; loadCurrent(); }, 350);
  });
  $("#sort-select").addEventListener("change", (e) => { state.sort = e.target.value; state.page = 1; loadPage(); });
  $("#toggle-filters").addEventListener("click", (e) => {
    const adv = $("#advanced-filters");
    adv.hidden = !adv.hidden;
    e.currentTarget.classList.toggle("active", !adv.hidden);
    e.currentTarget.setAttribute("aria-expanded", String(!adv.hidden));
  });
  const FILTER_INPUTS = { durationMin: "#f-dur-min", durationMax: "#f-dur-max", viewsMin: "#f-views-min",
    viewsMax: "#f-views-max", dateFrom: "#f-date-from", dateTo: "#f-date-to" };
  $("#apply-filters").addEventListener("click", () => {
    for (const [key, sel] of Object.entries(FILTER_INPUTS)) state.filters[key] = $(sel).value.trim();
    state.page = 1;
    loadPage();
  });
  $("#reset-filters").addEventListener("click", () => {
    for (const sel of Object.values(FILTER_INPUTS)) $(sel).value = "";
    state.filters = { ...EMPTY_FILTERS };
    state.page = 1;
    loadPage();
  });
}

function bindSettings() {
  const modal = $("#settings-modal");
  $("#settings-btn").addEventListener("click", () => { openModal(modal); checkHealth(); });
  $("#settings-close").addEventListener("click", () => closeModal(modal));

  $("#sb-mode-seg").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    state.sbMode = b.dataset.mode === "mark" ? "mark" : "remove";
    renderSbMode();
    persistChoices();
  }));
  const subs = () => saveSettings({
    subtitles: $("#subs-enabled").checked, subtitle_langs: $("#subs-langs").value.trim() || "en",
    subtitle_auto: $("#subs-auto").checked, subtitle_embed: $("#subs-embed").checked,
  });
  for (const id of ["#subs-enabled", "#subs-langs", "#subs-auto", "#subs-embed"]) $(id).addEventListener("change", subs);

  const embeds = () => saveSettings({
    embed_metadata: $("#embed-metadata").checked, embed_thumbnail: $("#embed-thumbnail").checked,
    embed_chapters: $("#embed-chapters").checked,
  });
  for (const id of ["#embed-metadata", "#embed-thumbnail", "#embed-chapters"]) $(id).addEventListener("change", embeds);
  $("#use-archive").addEventListener("change", (e) => saveSettings({ use_archive: e.target.checked }));
  $("#cookies-browser").addEventListener("change", (e) => saveSettings({ cookies_browser: e.target.value }));
  $("#cookies-file").addEventListener("change", (e) => saveSettings({ cookies_file: e.target.value.trim() }));
  $("#concurrency-input").addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10);
    if (!Number.isFinite(v)) return;
    const n = Math.max(1, Math.min(state.maxConcurrency, v));
    e.target.value = String(n);
    saveSettings({ concurrency: n });
  });
  $("#dest-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#dest-input").value.trim();
    if (v) saveSettings({ download_dir: v }, "Destination updated.");
  });
  $("#update-ytdlp").addEventListener("click", updateYtdlp);
  $("#music-keep-full").addEventListener("change", (e) => saveSettings({ music_keep_full: e.target.checked }));
  $("#sync-hours").addEventListener("change", (e) => saveSettings({ podcast_sync_hours: Number(e.target.value) }));
}

function bindFollows() {
  $("#follow-btn").addEventListener("click", () => {
    if (!state.podcast) return;
    const on = state.follows.some((f) => f.feed_url === state.podcast.feed_url);
    setFollow(state.podcast, !on);
  });
  $("#sync-all").addEventListener("click", () => syncFollows());
  $("#opml-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) importOpml(file);
    e.target.value = "";
  });
}

function bindQueue() {
  $("#q-start").addEventListener("click", () => post("/api/queue/start").catch((err) => toast(err.message, "error")));
  $("#q-pause").addEventListener("click", () => post("/api/queue/pause").catch((err) => toast(err.message, "error")));
  $("#q-clear").addEventListener("click", clearFinished);
}

async function init() {
  initTheme();
  document.documentElement.dataset.mode = state.mode;
  initModals();
  initLogViewer();
  initLibrary();
  initNotifications();
  bindSource();
  bindSettings();
  bindFollows();
  bindQueue();
  bindViews();
  initEasterEggs();
  freshEmptyLine();
  try { applySettings(await api("/api/settings")); } catch (err) { toast("Could not load settings.", "error"); }
  connectEvents();
  checkHealth();
  // Re-check the toolchain so installing yt-dlp/ffmpeg mid-session clears
  // the warning without a reload (versions are cached server-side).
  setInterval(checkHealth, 30000);
  window.addEventListener("focus", checkHealth);
}

document.addEventListener("DOMContentLoaded", init);
