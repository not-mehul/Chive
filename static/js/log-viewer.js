// The per-item log window. Follows a running download live (polling every
// LOG_FOLLOW_MS), keeps your scroll position if you've scrolled up to read,
// and never lets a slow response for a previous item overwrite the current one.

import { getJSON } from "./api.js";
import { $, el, toast } from "./dom.js";
import { safeFileName } from "./format.js";
import { closeModal, openModal } from "./modal.js";

const LOG_FOLLOW_MS = 1500;
let current = { id: null, title: "", text: "", token: 0, timer: null };

function overlay() { return $("#log-modal"); }

function stop() {
  clearTimeout(current.timer);
  current.token++;          // any in-flight refresh is now stale
}

function render(text, following, placeholder) {
  const body = $("#log-modal-body");
  const live = $("#log-modal-live");
  if (live) live.hidden = !following;
  if (!text) {
    body.textContent = placeholder;
  } else if (text !== current.text) {
    // Stick to the bottom only if the reader hasn't scrolled up.
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
    body.textContent = text;
    if (atBottom || !current.text) body.scrollTop = body.scrollHeight;
  }
  current.text = text;
}

async function refresh(token) {
  if (token !== current.token) return;
  let data;
  try {
    data = await getJSON(`/api/queue/log?id=${encodeURIComponent(current.id)}`);
  } catch (err) {
    if (token !== current.token) return;
    if (!current.text) $("#log-modal-body").textContent = `Could not load the log: ${err.message || err}.`;
    schedule(token);        // transient while following — try again
    return;
  }
  if (token !== current.token) return;
  const following = data.status === "downloading" || data.status === "pending";
  const text = data.available && data.lines.length ? data.lines.join("\n") : "";
  render(text, following, following
    ? "(waiting for output…)"
    : "(no log recorded for this item yet — it appears once the download starts)");
  if (following) schedule(token);
}

function schedule(token) {
  clearTimeout(current.timer);
  current.timer = setTimeout(() => refresh(token), LOG_FOLLOW_MS);
}

/** Open the log for a queue item. */
export function openLog(id, title) {
  stop();
  current = { id, title, text: "", token: current.token + 1, timer: null };
  $("#log-modal-title").textContent = `Log · ${title}`;
  $("#log-modal-body").textContent = "Loading…";
  openModal(overlay(), { onClose: stop });
  refresh(current.token);
}

/** Show one-off output (e.g. a yt-dlp update) in the same window. */
export function showStaticLog(title, text) {
  stop();
  current = { id: null, title, text: "", token: current.token + 1, timer: null };
  $("#log-modal-title").textContent = title;
  openModal(overlay(), { onClose: stop });
  render(text, false, "(no output)");
}

function saveLog() {
  if (!current.text) { toast("Nothing to save yet.", "notice"); return; }
  const blob = new Blob([current.text], { type: "text/plain" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `chive-${safeFileName(current.title)}.log` });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

export function initLogViewer() {
  $("#log-modal-close").addEventListener("click", () => closeModal(overlay()));
  $("#log-modal-save").addEventListener("click", saveLog);
}
