import { validateConfig } from "./_supabase.js";

// Shared request wrapper for the Supabase-backed dashboard endpoints.
//
// Every write/read endpoint repeated the same preamble: permissive CORS, the
// OPTIONS preflight, a single-method guard, the Supabase env check, and the
// config-warning log. This centralises that boilerplate so each handler keeps
// only its business logic. (api/vacancies.js is intentionally NOT built on this
// — it is same-origin-only with a fail-closed auth gate, a different contract.)
//
// Behaviour is byte-for-byte what the hand-written handlers did: same status
// codes, same headers, same log strings (the `${label}:` prefix reproduces each
// endpoint's old messages).
export function withHandler({ method, label }, fn) {
  const allowHeaders =
    method === "POST" ? "Content-Type, Authorization" : "Authorization";

  return async function handler(req, res) {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", `${method}, OPTIONS`);
    res.setHeader("Access-Control-Allow-Headers", allowHeaders);

    if (req.method === "OPTIONS") return res.status(204).end();
    if (req.method !== method)
      return res.status(405).json({ error: "Method not allowed" });

    if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
      console.error(
        `${label}: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY`,
      );
      return res.status(500).json({ error: "Server misconfigured" });
    }

    const warnings = validateConfig();
    if (warnings.length)
      console.warn(`${label}: config warning —`, warnings.join("; "));

    return fn(req, res);
  };
}
