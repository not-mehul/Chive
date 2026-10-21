// Pure display formatting — no DOM, so it is unit-tested under Node.

export function plural(n, one, many = one + "s") {
  return `${n} ${n === 1 ? one : many}`;
}

/** Seconds → "m:ss" or "h:mm:ss"; "—" when unknown. */
export function fmtDuration(s) {
  if (s == null || !isFinite(s)) return "—";
  const n = Math.max(0, Math.floor(s));
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const ss = String(n % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** "YYYYMMDD" → "YYYY·MM·DD"; "" for anything else. */
export function fmtDate(yyyymmdd) {
  if (typeof yyyymmdd !== "string" || !/^\d{8}$/.test(yyyymmdd)) return "";
  return `${yyyymmdd.slice(0, 4)}·${yyyymmdd.slice(4, 6)}·${yyyymmdd.slice(6, 8)}`;
}

export function fmtViews(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

export function fmtBytes(n) {
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, f = Number(n);
  while (f >= 1024 && i < units.length - 1) { f /= 1024; i++; }
  return (i === 0 ? `${Math.round(f)}` : f.toFixed(1)) + " " + units[i];
}

/** Seconds → "1h 2m" / "3m 4s" / "5s"; "—" when unknown. */
export function fmtClock(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${s}s`;
}

/** A filesystem-friendly name for a downloaded log. */
export function safeFileName(title, fallback = "log") {
  return (title || fallback).replace(/[^\w.-]+/g, "_").slice(0, 60) || fallback;
}
