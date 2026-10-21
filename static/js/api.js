// The bridge's HTTP API. Every request is same-origin JSON — the server
// refuses state-changing requests that aren't (its cross-site guard).

/** POST (or any method) a JSON body; resolves to the parsed JSON response. */
export async function api(path, opts = {}) {
  const resp = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error((data && data.error) || `request failed (${resp.status})`);
  return data;
}

export const post = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

/** GET JSON, retrying once: a request can be dropped while the server is busy
 *  streaming events and a download at the same moment. */
export async function getJSON(path) {
  let resp;
  try {
    resp = await fetch(path, { cache: "no-store" });
  } catch (e) {
    await new Promise((r) => setTimeout(r, 400));
    resp = await fetch(path, { cache: "no-store" });
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error((data && data.error) || `server returned HTTP ${resp.status}`);
  return data;
}
