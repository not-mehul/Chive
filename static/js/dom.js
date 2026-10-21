// Small DOM helpers shared by every module.

export const $ = (sel) => document.querySelector(sel);

/** Create an element: el("button", { class: "x", onclick: fn }, ["text", node]). */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === false || v == null) continue;
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

/** An inline stroke icon from SVG path markup (our own constants only). */
export function svgEl(inner, size = 14) {
  const wrap = document.createElement("span");
  wrap.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
  return wrap.firstChild;
}

export const ICONS = {
  check: `<path d="m5 12 5 5L20 7"/>`,
  pause: `<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>`,
  play: `<polygon points="6 4 20 12 6 20 6 4"/>`,
  stop: `<rect x="6" y="6" width="12" height="12"/>`,
  retry: `<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>`,
  folder: `<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>`,
  log: `<path d="M4 6h16M4 12h16M4 18h10"/>`,
  close: `<path d="M18 6 6 18M6 6l12 12"/>`,
  download: `<path d="M12 5v14M5 12h7l3 3 3-3"/>`,
  alert: `<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h0"/>`,
  clock: `<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>`,
  search: `<circle cx="11" cy="11" r="7"/><path d="m20 20-3-3"/>`,
};

/** Find an element by a data-id, safely escaping arbitrary ids (podcast
 *  guids can contain quotes or backslashes). */
export function byDataId(selector, id) {
  return document.querySelector(`${selector}[data-id="${CSS.escape(String(id))}"]`);
}

let toastTimer = null;
export function toast(msg, kind = "") {
  const existing = $(".toast");
  if (existing) existing.remove();
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, msg);
  document.body.appendChild(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 4200);
}
