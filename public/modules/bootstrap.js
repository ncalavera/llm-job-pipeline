// bootstrap.js — populate window.VACANCY_DATA, then load the app.
//
// state.js destructures window.VACANCY_DATA synchronously at module-eval, and
// every module reads those bindings. So the data must exist BEFORE the app's
// module graph loads. This loader fetches it first, then dynamically imports
// app.js — keeping state.js, catalog.js, and every consumer unchanged.
//
// Source selection:
//   200          -> live payload from /api/vacancies (full mode)
//   404          -> endpoint absent (simple/local mode) -> baked data.js
//   401/500/503  -> a real error (auth gate, misconfig, snapshot missing) ->
//                   show an explicit message, NEVER a silent stale fallback
//   file://      -> offline view, no API host -> baked data.js
//
// Auto-refresh (full mode only): once live, poll /api/vacancies every
// POLL_INTERVAL_MS with the last response's ETag as If-None-Match. Most polls
// come back 304 (snapshot unchanged) — a few dozen bytes, no body to parse.
// On an actual change, state.applySnapshot() hot-swaps the new data into the
// SAME array/object references every render function already reads, so
// scheduleRender() repaints without a reload and without touching state.js's
// UI state (active tab, filters, sort, scroll) — see state.js for why that's
// safe. Polling pauses while the tab is hidden (Page Visibility API) and
// catches up immediately on refocus instead of waiting out the interval.

const POLL_INTERVAL_MS = 60_000; // 60s — live-feeling, and an unchanged poll is nearly free

/**
 * Decide where the dashboard payload comes from, given an HTTP response to
 * GET /api/vacancies. Pure — unit-tested without a browser.
 * @returns {"live"|"fallback"|"reauth"|"error"}
 */
export function resolveSource({ ok, status }) {
  if (ok) return "live";
  if (status === 404) return "fallback"; // endpoint not deployed → simple/local mode
  if (status === 401) return "reauth"; // session cookie expired/cleared → re-login
  return "error"; // 500 / 503 → real failure, do not mask with stale data
}

function isHttp() {
  return location.protocol === "http:" || location.protocol === "https:";
}

/**
 * Decide whether a poll response carries data to apply. Pure — unit-tested
 * without a browser. 304 (unchanged) and any non-2xx (a background poll
 * failing should never disrupt the open dashboard the way boot()'s hard
 * error path does — just try again next tick) both mean "nothing to apply".
 * @returns {boolean}
 */
export function shouldApplyPollResponse(status) {
  return status >= 200 && status < 300;
}

/**
 * Poll /api/vacancies for a changed snapshot and hot-swap it in when found.
 * No-ops outside full mode (no endpoint) — callers only invoke this after a
 * "live" initial load. Exported for tests; real use is the `boot()` call below.
 * @param {string|null} initialETag - ETag from the initial live fetch in boot().
 */
export function startPolling(initialETag) {
  let etag = initialETag || null;
  let timer = null;

  async function tick() {
    let res;
    try {
      res = await fetch("/api/vacancies", {
        headers: etag
          ? { Accept: "application/json", "If-None-Match": etag }
          : { Accept: "application/json" },
      });
    } catch {
      return; // offline blip — next tick retries
    }
    if (!shouldApplyPollResponse(res.status)) return; // 304 or a poll-time error
    const nextETag = res.headers.get("ETag");
    let payload;
    try {
      payload = await res.json();
    } catch {
      return;
    }
    if (nextETag) etag = nextETag;
    try {
      const { applySnapshot, scheduleRender } = await import("./state.js");
      if (applySnapshot(payload)) scheduleRender();
    } catch {
      return; // a background poll must never disrupt the visible dashboard
    }
  }

  function startInterval() {
    if (timer) return;
    timer = setInterval(tick, POLL_INTERVAL_MS);
  }
  function stopInterval() {
    if (!timer) return;
    clearInterval(timer);
    timer = null;
  }

  if (document.visibilityState === "visible") startInterval();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      tick(); // catch up on whatever changed while the tab was hidden
      startInterval();
    } else {
      stopInterval();
    }
  });
}

function loadStaticDataJs() {
  // Classic <script> so its `var VACANCY_DATA = …` lands on window, exactly as
  // the old index.html did. Resolves once the global is populated.
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "data.js";
    s.onload = resolve;
    s.onerror = () => reject(new Error("failed to load data.js"));
    document.body.appendChild(s);
  });
}

function showError(detail) {
  const el = document.createElement("div");
  el.style.cssText =
    "max-width:42rem;margin:4rem auto;padding:1.5rem;font-family:system-ui,sans-serif;" +
    "border:1px solid #f5c2c7;background:#fff5f5;color:#842029;border-radius:8px;";
  el.innerHTML =
    "<h2 style='margin:0 0 .5rem'>Dashboard unavailable</h2>" +
    "<p style='margin:0'>Could not load live data (" +
    String(detail) +
    "). The data source is reachable but did not return a dashboard. " +
    "Showing stale data was deliberately avoided.</p>";
  document.body.prepend(el);
}

export async function boot() {
  let source = "fallback";
  let payload = null;
  let etag = null;

  if (isHttp()) {
    // One retry: a single transient network hiccup on a healthy deploy should
    // not blank the dashboard.
    let res = null;
    for (let attempt = 0; attempt < 2 && !res; attempt++) {
      try {
        res = await fetch("/api/vacancies", {
          headers: { Accept: "application/json" },
        });
      } catch {
        if (attempt === 1) source = "error"; // both tries failed
      }
    }
    if (res) {
      source = resolveSource({ ok: res.ok, status: res.status });
      if (source === "live") {
        etag = res.headers.get("ETag");
        payload = await res.json();
        // A 200 with a null/non-object body would crash app.js with a confusing
        // error — treat it as a failure, not live data.
        if (!payload || typeof payload !== "object") source = "error";
      }
    }
  }
  // file:// (offline view) keeps source = "fallback" → baked data.js.

  if (source === "reauth") {
    // A fetch() 401 does NOT trigger the browser's auth dialog — only a
    // top-level navigation does. Reload so an expired session prompts a login
    // instead of stranding the user on a dead error screen.
    location.reload();
    return;
  }

  if (source === "error") {
    showError("the live endpoint returned an error");
    return;
  }

  if (source === "live") {
    window.VACANCY_DATA = payload;
  } else {
    try {
      await loadStaticDataJs();
    } catch {
      showError("data.js missing");
      return;
    }
  }

  await import("../app.js"); // app.js lives at public/app.js, one level up

  // Only full mode has a live endpoint to poll — fallback (simple/local mode,
  // baked data.js) has no /api/vacancies to ask.
  if (source === "live") startPolling(etag);
}

// Only run in the browser; importing this module under a test runner (no
// window) just exposes resolveSource for unit testing.
if (typeof window !== "undefined") {
  boot();
}
