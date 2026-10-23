// The Library view: browse, search, filter, play and delete what's archived,
// plus the local podcast feeds for archived shows.

import { getJSON, post } from "./api.js";
import { $, ICONS, el, svgEl, toast } from "./dom.js";
import { fmtBytes, fmtDuration, plural } from "./format.js";
import { closeModal, openModal } from "./modal.js";

const PAGE = 60;
const SOURCE_LABELS = { "": "All", youtube: "YouTube", podcast: "Podcasts", music: "Music", web: "Web", other: "Other" };
const lib = { q: "", source: "", sort: "recent", items: [], total: 0, counts: {}, stale: true, token: 0 };

const fileUrl = (rel) => `/api/library/file?path=${encodeURIComponent(rel)}`;

function isVisible() {
  return !$("#view-library").hidden;
}

/** Fetch a page; `reset` starts over (new search/filter/sort). */
async function load(reset = true) {
  const token = ++lib.token;
  const offset = reset ? 0 : lib.items.length;
  const params = new URLSearchParams({ q: lib.q, source: lib.source, sort: lib.sort, offset, limit: PAGE });
  let data;
  try {
    data = await getJSON(`/api/library?${params}`);
  } catch (err) {
    toast(`Could not load the library: ${err.message}`, "error");
    return;
  }
  if (token !== lib.token) return;           // a newer request superseded this one
  lib.items = reset ? data.items : lib.items.concat(data.items);
  lib.total = data.total;
  lib.counts = data.counts;
  lib.stale = false;
  render(data);
}

function render(data) {
  $("#lib-total").textContent = String(lib.total);
  $("#lib-meta").textContent = lib.total === 1 ? "file" : "files";
  $("#lib-size").textContent = data.total_size ? fmtBytes(data.total_size) : "";
  renderSources();
  const grid = $("#lib-grid");
  grid.replaceChildren(...lib.items.map(card));
  const empty = $("#lib-empty");
  empty.hidden = lib.items.length > 0;
  empty.textContent = lib.q || lib.source
    ? "Nothing in your library matches."
    : "Your library is empty — finished downloads appear here.";
  $("#lib-more").hidden = lib.items.length >= lib.total;
  $("#lib-feeds").hidden = lib.source !== "podcast";
  if (lib.source === "podcast") loadFeeds();
}

function renderSources() {
  const box = $("#lib-sources");
  const all = Object.values(lib.counts).reduce((a, b) => a + b, 0);
  box.replaceChildren(...Object.entries(SOURCE_LABELS)
    .filter(([id]) => id === "" || lib.counts[id])
    .map(([id, label]) => el("button", {
      type: "button", role: "radio", class: `chip${lib.source === id ? " active" : ""}`,
      "aria-checked": lib.source === id ? "true" : "false",
      onclick: () => { lib.source = id; load(true); },
    }, [label, el("span", { class: "chip-count mono" }, String(id ? lib.counts[id] : all))])));
}

function art(it) {
  if (it.cover) return el("img", { src: fileUrl(it.cover), alt: "", loading: "lazy" });
  if (it.thumbnail) return el("img", { src: it.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
  return el("span", { class: "lib-art-blank" }, [svgEl(it.kind === "video" ? ICONS.play : ICONS.note, 22)]);
}

function card(it) {
  const meta = [
    it.track ? `#${it.track}` : null,
    it.artist && it.artist !== it.collection ? it.artist : null,
    it.collection,
    it.duration ? fmtDuration(it.duration) : null,
    fmtBytes(it.size),
  ].filter(Boolean).join(" · ");
  return el("article", { class: `lib-card ${it.kind}`, "data-path": it.path }, [
    el("button", { type: "button", class: "lib-art", title: `Play ${it.title}`, "aria-label": `Play ${it.title}`,
      onclick: () => play(it) }, [art(it), el("span", { class: "lib-play" }, [svgEl(ICONS.play, 18)])]),
    el("div", { class: "lib-body" }, [
      el("h3", { class: "lib-title", title: it.title }, it.title),
      el("div", { class: "lib-meta" }, meta),
      el("div", { class: "lib-foot" }, [
        el("span", { class: `qchip lib-src src-${it.source}` }, SOURCE_LABELS[it.source] || it.source),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "lib-action", title: "Play", "aria-label": `Play ${it.title}`,
          onclick: () => play(it) }, svgEl(ICONS.play)),
        el("a", { class: "lib-action", href: fileUrl(it.path), download: "", title: "Save a copy",
          "aria-label": `Save a copy of ${it.title}` }, svgEl(ICONS.save)),
        el("button", { type: "button", class: "lib-action danger", title: "Delete from disk",
          "aria-label": `Delete ${it.title}`, onclick: () => remove(it) }, svgEl(ICONS.trash)),
      ]),
    ]),
  ]);
}

function play(it) {
  const body = $("#player-body");
  const src = fileUrl(it.path);
  const media = it.kind === "video"
    ? el("video", { src, controls: true, autoplay: true, preload: "metadata", playsinline: true })
    : el("audio", { src, controls: true, autoplay: true, preload: "metadata" });
  const cover = it.cover ? el("img", { class: "player-cover", src: fileUrl(it.cover), alt: "" })
    : (it.thumbnail && it.kind !== "video" ? el("img", { class: "player-cover", src: it.thumbnail, alt: "" }) : null);
  media.addEventListener("error", () => {
    body.appendChild(el("p", { class: "hint is-error" },
      "This browser can't play that format. Use “Save a copy” to open it in another player."));
  }, { once: true });
  body.replaceChildren(...[cover, media, el("p", { class: "hint player-meta" },
    [it.collection, it.artist].filter(Boolean).join(" · "))].filter(Boolean));
  $("#player-title").textContent = it.title;
  openModal($("#player-modal"), {
    onClose: () => { media.pause(); media.removeAttribute("src"); media.load(); body.replaceChildren(); },
  });
}

async function remove(it) {
  if (!confirm(`Delete “${it.title}” from disk?\n\nThis can't be undone. It will also be removed from the download archive, so it can be downloaded again.`)) return;
  try {
    const r = await post("/api/library/delete", { path: it.path });
    toast(`Deleted ${plural(r.removed.length, "file")}.`);
    load(true);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function loadFeeds() {
  const list = $("#lib-feed-list");
  try {
    const { shows } = await getJSON("/api/library/shows");
    list.replaceChildren(...shows.map((s) => el("div", { class: "lib-feed" }, [
      el("span", { class: "lib-feed-name" }, s.name),
      el("span", { class: "muted mono" }, plural(s.episodes, "episode")),
      el("code", { class: "lib-feed-url mono" }, s.feed_url),
      el("button", {
        type: "button", class: "btn btn-ghost btn-mini",
        onclick: async () => {
          try { await navigator.clipboard.writeText(s.feed_url); toast("Feed URL copied."); }
          catch (e) { toast("Copy failed — select the URL and copy it manually.", "notice"); }
        },
      }, "Copy URL"),
    ])));
    if (!shows.length) list.replaceChildren(el("p", { class: "muted" }, "No archived podcast episodes yet."));
  } catch (err) {
    list.replaceChildren(el("p", { class: "muted" }, err.message));
  }
}

/** Refresh the tab badge, and the view itself if it's showing. */
let refreshTimer = null;
export function onLibraryChanged() {
  lib.stale = true;
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    if (isVisible()) await load(true);
    else refreshCount();
  }, 400);
}

async function refreshCount() {
  try {
    const d = await getJSON("/api/library?limit=1");
    $("#library-count").textContent = d.total ? String(d.total) : "";
  } catch (e) { /* the badge is cosmetic */ }
}

export function showLibrary() {
  if (lib.stale) load(true);
}

export function focusLibrarySearch() {
  $("#lib-search").focus();
}

export function initLibrary() {
  let timer = null;
  $("#lib-search").addEventListener("input", (e) => {
    lib.q = e.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => load(true), 250);
  });
  $("#lib-sort").addEventListener("change", (e) => { lib.sort = e.target.value; load(true); });
  $("#lib-more").addEventListener("click", () => load(false));
  $("#player-close").addEventListener("click", () => closeModal($("#player-modal")));
  refreshCount();
}
