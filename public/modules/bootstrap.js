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

/**
 * Decide where the dashboard payload comes from, given an HTTP response to
 * GET /api/vacancies. Pure — unit-tested without a browser.
 * @returns {"live"|"fallback"|"error"}
 */
export function resolveSource({ ok, status }) {
  if (ok) return "live";
  if (status === 404) return "fallback"; // endpoint not deployed → simple/local mode
  return "error"; // 401 / 500 / 503 → real failure, do not mask with stale data
}

function isHttp() {
  return location.protocol === "http:" || location.protocol === "https:";
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

  if (isHttp()) {
    try {
      const res = await fetch("/api/vacancies", {
        headers: { Accept: "application/json" },
      });
      source = resolveSource({ ok: res.ok, status: res.status });
      if (source === "live") payload = await res.json();
    } catch {
      source = "error"; // network failure reaching the API host
    }
  }
  // file:// (offline view) keeps source = "fallback" → baked data.js.

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
}

// Only run in the browser; importing this module under a test runner (no
// window) just exposes resolveSource for unit testing.
if (typeof window !== "undefined") {
  boot();
}
