// Edge Middleware — optional Basic Auth gate for the dashboard.
// Set AUTH_USER and AUTH_PASS in your Vercel project to require a login.
// If either is unset, the middleware passes every request through, so a fresh
// public deployment is open by default and you opt into auth by adding the vars.
//
// Runtime: Vercel Edge (vanilla, NOT Next.js — no next/server available).
//
// Flow when auth is enabled:
// 1. __Host-session cookie → HMAC verify → pass through.
// 2. Authorization: Basic → verify → 302 redirect that sets the session cookie.
// 3. Neither → 401 with a WWW-Authenticate challenge.

export const config = { matcher: "/((?!_next/static|favicon.ico).*)" };

const COOKIE_NAME = "__Host-session";
const MAX_AGE = 31536000; // 1 year in seconds
const CLOCK_SKEW = 60; // 60s tolerance

// --- Crypto helpers (Web Crypto API, available on Vercel Edge) ---

async function hmacSign(message, secret) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function hmacVerify(message, signature, secret) {
  const expected = await hmacSign(message, secret);
  if (expected.length !== signature.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  }
  return diff === 0;
}

// --- Cookie helpers ---

async function buildSessionToken(user, secret) {
  const expiry = Math.floor(Date.now() / 1000) + MAX_AGE;
  const payload = `${user}:${expiry}`;
  const sig = await hmacSign(payload, secret);
  return `${payload}.${sig}`;
}

function parseSessionToken(token) {
  if (!token) return null;
  const lastDot = token.lastIndexOf(".");
  if (lastDot === -1) return null;
  const payload = token.substring(0, lastDot);
  const signature = token.substring(lastDot + 1);
  const parts = payload.split(":");
  if (parts.length !== 2) return null;
  const [user, expiryStr] = parts;
  const expiry = parseInt(expiryStr, 10);
  if (isNaN(expiry)) return null;
  return { user, expiry, payload, signature };
}

/** Parse a named cookie from the Cookie header string. */
function getCookie(request, name) {
  const header = request.headers.get("cookie");
  if (!header) return null;
  const match = header.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : null;
}

function clearCookieHeader() {
  return `${COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`;
}

function setCookieHeader(token) {
  return `${COOKIE_NAME}=${token}; Path=/; Max-Age=${MAX_AGE}; HttpOnly; Secure; SameSite=Lax`;
}

// --- Main middleware ---

export default async function middleware(request) {
  const authUser = process.env.AUTH_USER;
  const authPass = process.env.AUTH_PASS;

  // Auth not configured → open deployment, pass everything through.
  if (!authUser || !authPass) {
    return;
  }

  // 1. Check session cookie
  const cookieValue = getCookie(request, COOKIE_NAME);
  if (cookieValue) {
    const parsed = parseSessionToken(cookieValue);
    if (parsed) {
      const now = Math.floor(Date.now() / 1000);
      if (parsed.expiry + CLOCK_SKEW > now && parsed.user === authUser) {
        const valid = await hmacVerify(
          parsed.payload,
          parsed.signature,
          authPass,
        );
        if (valid) {
          return; // authenticated — pass through to origin
        }
      }
    }
    // Invalid/expired/corrupted cookie — clear it and re-challenge
    return new Response("Authentication required", {
      status: 401,
      headers: {
        "WWW-Authenticate": 'Basic realm="Job Vacancy Dashboard"',
        "Set-Cookie": clearCookieHeader(),
      },
    });
  }

  // 2. Check Basic Auth header
  const auth = request.headers.get("authorization");
  if (auth) {
    const [scheme, encoded] = auth.split(" ");
    if (scheme === "Basic" && encoded) {
      try {
        const decoded = atob(encoded);
        const colonIdx = decoded.indexOf(":");
        if (colonIdx !== -1) {
          const user = decoded.substring(0, colonIdx);
          const pass = decoded.substring(colonIdx + 1);
          if (user === authUser && pass === authPass) {
            // Vanilla Vercel Edge can't set response headers on pass-through,
            // so redirect to the same URL with the session cookie set.
            const token = await buildSessionToken(user, authPass);
            return new Response(null, {
              status: 302,
              headers: {
                Location: request.url,
                "Set-Cookie": setCookieHeader(token),
              },
            });
          }
        }
      } catch {
        // Invalid base64 — fall through to 401
      }
    }
  }

  // 3. No valid auth — challenge
  return new Response("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Job Vacancy Dashboard"' },
  });
}
