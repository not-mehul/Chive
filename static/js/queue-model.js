// The queue as data: applying server events and deriving stats. Pure — no
// DOM — so the event logic is unit-tested under Node.

const ACTIVE = new Set(["pending", "downloading", "paused"]);
const FINISHED = new Set(["completed", "failed", "cancelled"]);

/**
 * Apply one SSE payload to `queue` (an array of items). Returns the new queue
 * and what the UI must redraw:
 *   "all"      — rebuild the queue list (and grid)
 *   "item"     — patch one row (`item`); `statusChanged` if its status moved
 *   "removed"  — drop rows (`ids`)
 *   null       — nothing queue-related changed
 */
export function applyQueueEvent(queue, payload) {
  switch (payload && payload.type) {
    case "snapshot":
      return { queue: payload.items || [], redraw: "all" };
    case "queued": {
      const items = payload.items || (payload.item ? [payload.item] : []);
      if (!items.length) return { queue, redraw: null };
      const next = queue.slice();
      const index = new Map(next.map((x, ix) => [x.id, ix]));
      for (const it of items) {
        const ix = index.get(it.id);
        if (ix != null) next[ix] = it;
        else { index.set(it.id, next.length); next.push(it); }
      }
      return { queue: next, redraw: "all" };
    }
    case "update":
    case "progress": {
      const it = payload.item;
      if (!it) return { queue, redraw: null };
      const ix = queue.findIndex((x) => x.id === it.id);
      if (ix < 0) return { queue: queue.concat([it]), redraw: "all" };
      const next = queue.slice();
      const statusChanged = next[ix].status !== it.status;
      next[ix] = it;
      return { queue: next, redraw: "item", item: it, statusChanged };
    }
    case "removed":
      return { queue: queue.filter((x) => x.id !== payload.id), redraw: "removed", ids: [payload.id] };
    case "cleared": {
      const ids = payload.ids || queue.filter((x) => FINISHED.has(x.status)).map((x) => x.id);
      const gone = new Set(ids);
      return { queue: queue.filter((x) => !gone.has(x.id)), redraw: "removed", ids };
    }
    default:
      return { queue, redraw: null };
  }
}

/** Video ids currently in the queue (pending, downloading or paused). */
export function queuedVideoIds(queue) {
  return new Set(queue.filter((i) => ACTIVE.has(i.status)).map((i) => i.video_id));
}

/** Headline numbers for the stats bar. */
export function queueStats(queue) {
  let speed = 0, archived = 0, remainingBytes = 0, measurable = false;
  let active = 0, waiting = 0, done = 0, failed = 0;
  for (const it of queue) {
    if (it.status === "downloading") {
      active++;
      if (it.speed_bps) speed += it.speed_bps;
      if (it.downloaded_bytes) archived += it.downloaded_bytes;
      if (it.total_bytes && it.downloaded_bytes != null) {
        remainingBytes += Math.max(0, it.total_bytes - it.downloaded_bytes);
        measurable = true;
      }
    } else if (it.status === "pending" || it.status === "paused") {
      waiting++;
    } else if (it.status === "completed") {
      done++;
      if (it.total_bytes) archived += it.total_bytes;
    } else {
      failed++;
    }
  }
  return {
    remaining: active + waiting, active, done, failed, speed, archived,
    eta: measurable && speed > 0 ? remainingBytes / speed : null,
  };
}

/** The status wording on a queue row. */
export function statusText(it) {
  switch (it.status) {
    case "pending": return "Queued";
    case "downloading": return it.message || "Downloading";
    case "paused": return "Paused";
    case "completed": return it.message && it.message !== "Saved" ? `Completed · ${it.message}` : "Completed";
    case "failed": return `Failed · ${it.message || ""}`.trim();
    case "cancelled": return "Cancelled";
    default: return it.status;
  }
}

/** True when a downloading item is in a phase with no meaningful percent. */
export function isIndeterminate(it, phases) {
  return it.status === "downloading" && phases.has(it.message || "");
}
