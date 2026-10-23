// Desktop notifications (opt-in, per browser) and the progress-in-tab-title.

import { $ } from "./dom.js";

const KEY = "chive-notify";
const BASE_TITLE = document.title;

function supported() {
  return typeof window !== "undefined" && "Notification" in window;
}

function stored() {
  try { return localStorage.getItem(KEY) === "on"; } catch (e) { return false; }
}

function store(on) {
  try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (e) { /* private mode */ }
}

function notificationsEnabled() {
  return supported() && stored() && Notification.permission === "granted";
}

/** Show a desktop notification — only while Chive is in a background tab
 *  (in the foreground the in-page toast already says it). */
export function notify(title, body = "", tag = undefined) {
  if (!notificationsEnabled() || !document.hidden) return;
  try {
    const n = new Notification(title, { body, tag, icon: "/static/favicon.svg" });
    n.onclick = () => { window.focus(); n.close(); };
  } catch (e) { /* some browsers only allow notifications from a service worker */ }
}

function renderToggle() {
  const box = $("#notify-toggle");
  const hint = $("#notify-hint");
  if (!supported()) {
    box.checked = false;
    box.disabled = true;
    hint.textContent = "This browser doesn't support desktop notifications.";
    return;
  }
  box.checked = notificationsEnabled();
  if (Notification.permission === "denied") {
    hint.textContent = "Notifications are blocked for this site — allow them in your browser's site settings.";
  }
}

export function initNotifications() {
  renderToggle();
  $("#notify-toggle").addEventListener("change", async (e) => {
    if (!e.target.checked) { store(false); return renderToggle(); }
    const permission = Notification.permission === "default"
      ? await Notification.requestPermission() : Notification.permission;
    store(permission === "granted");
    renderToggle();
  });
}

/** "↓ 42% · Chive" while downloading, so progress shows in the tab strip. */
export function updateTitle(queue) {
  const active = queue.filter((i) => i.status === "downloading");
  if (!active.length) { document.title = BASE_TITLE; return; }
  const pct = Math.round(active.reduce((s, i) => s + (i.progress || 0), 0) / active.length);
  document.title = `↓ ${pct}%${active.length > 1 ? ` (${active.length})` : ""} · Chive`;
}
