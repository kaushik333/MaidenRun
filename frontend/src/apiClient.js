// Signs every backend request with CONTROL_PLANE_SECRET (see
// backend/app/core/auth.py) so the backend can tell "a request from someone
// who holds the secret" apart from "anyone who can reach the port" — the
// secret itself is never sent, only a signature derived from it, so this
// stays meaningful even without TLS in front of the backend. A no-op drop-in
// replacement for fetch() when no secret is stored (the local/dev default).

const SECRET_STORAGE_KEY = "maidenrun-control-plane-secret";

export function getStoredSecret() {
  try {
    return localStorage.getItem(SECRET_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setStoredSecret(secret) {
  try {
    if (secret) localStorage.setItem(SECRET_STORAGE_KEY, secret);
    else localStorage.removeItem(SECRET_STORAGE_KEY);
  } catch {
    // Persistence is a convenience; signing still works for this tab via
    // the in-memory value the caller already holds.
  }
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return Array.from(new Uint8Array(signature), (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function apiFetch(url, options = {}) {
  const secret = getStoredSecret();
  if (!secret) return fetch(url, options);

  const method = (options.method || "GET").toUpperCase();
  const path = new URL(url, window.location.origin).pathname;
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const bodyHash = await sha256Hex(options.body || "");
  const signature = await hmacHex(secret, `${method}${path}${timestamp}${bodyHash}`);

  return fetch(url, {
    ...options,
    headers: { ...(options.headers || {}), "X-Timestamp": timestamp, "X-Signature": signature },
  });
}
