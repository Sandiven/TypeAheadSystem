/**
 * api.js
 * ------
 * WHAT: Thin wrappers around backend endpoints.
 *       Phase 5 adds fetchTrending() and fetchMetrics().
 */

const BASE_URL = import.meta.env.VITE_API_BASE ?? "/api";

export async function fetchSuggestions(prefix, signal) {
  const url = `${BASE_URL}/suggest?q=${encodeURIComponent(prefix)}`;
  const resp = await fetch(url, { signal });
  if (!resp.ok) throw new Error(`/suggest returned ${resp.status}`);
  return resp.json();
}

export async function submitSearch(query) {
  const resp = await fetch(`${BASE_URL}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail ?? `POST /search returned ${resp.status}`);
  }
  return resp.json();
}

/**
 * fetchTrending
 * -------------
 * Returns top 10 globally trending queries from GET /trending.
 * Called periodically by the Trending panel (every 10 seconds).
 */
export async function fetchTrending() {
  const resp = await fetch(`${BASE_URL}/trending?limit=10`);
  if (!resp.ok) throw new Error(`/trending returned ${resp.status}`);
  return resp.json();
}

/**
 * fetchMetrics
 * ------------
 * Returns the live performance report from GET /metrics.
 * Called periodically by the metrics sidebar (every 5 seconds).
 */
export async function fetchMetrics() {
  const resp = await fetch(`${BASE_URL}/metrics`);
  if (!resp.ok) throw new Error(`/metrics returned ${resp.status}`);
  return resp.json();
}
