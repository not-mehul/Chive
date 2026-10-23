// Easter eggs 🌱 — a console hello, the Konami code, and a blooming wordmark.

import { $, el, toast } from "./dom.js";

/** Purple-pompom confetti, as if a chive flower went to seed. */
function blossomBurst(n = 28) {
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const layer = el("div", { class: "blossom-layer", "aria-hidden": "true" });
  document.body.appendChild(layer);
  const glyphs = ["✿", "❀", "•", "✺", "🌱"];
  for (let i = 0; i < n; i++) {
    const p = el("span", { class: "blossom" }, glyphs[(Math.random() * glyphs.length) | 0]);
    p.style.left = Math.random() * 100 + "vw";
    p.style.animationDelay = Math.random() * 0.5 + "s";
    p.style.animationDuration = 2.4 + Math.random() * 1.8 + "s";
    p.style.fontSize = 0.7 + Math.random() * 1.1 + "rem";
    p.style.setProperty("--drift", (Math.random() * 2 - 1) * 140 + "px");
    layer.appendChild(p);
  }
  setTimeout(() => layer.remove(), 4800);
}

const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight",
  "ArrowLeft", "ArrowRight", "b", "a"];

export function initEasterEggs() {
  try {
    console.log("%c🌱 Chive %c— ar·chive everything, snip the fluff.",
      "font-weight:bold;color:#84c95c", "color:#9a8");
    console.log("%cpsst — try the Konami code: ↑ ↑ ↓ ↓ ← → ← → b a", "color:#c9a8e0;font-style:italic");
  } catch (e) { /* no console */ }

  let progress = 0;
  window.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;   // never hijack typing
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    progress = key === KONAMI[progress] ? progress + 1 : (key === KONAMI[0] ? 1 : 0);
    if (progress === KONAMI.length) {
      progress = 0;
      blossomBurst(60);
      toast("🌿 Secret garden unlocked — go on, archive something lovely.");
    }
  });

  const brand = $("#brand");
  if (!brand) return;
  let taps = 0, tapTimer = null;
  brand.addEventListener("click", () => {
    brand.classList.remove("bloom");
    void brand.offsetWidth;            // restart the animation
    brand.classList.add("bloom");
    taps++;
    clearTimeout(tapTimer);
    tapTimer = setTimeout(() => { taps = 0; }, 1200);
    if (taps >= 5) {
      taps = 0;
      blossomBurst(40);
      toast("🌱 you grew a whole chive. impressive.");
    }
  });
}
