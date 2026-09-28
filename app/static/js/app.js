/* ==========================================================================
   Shared front-end helpers: toasts, fetch wrapper, hashing, formatting.
   No framework, no build step -- plain ES2020 that any browser runs directly.
   ========================================================================== */
"use strict";

/* ------------------------------------------------------------------ toasts */
function toast(message, kind = "info", timeout = 4200) {
  const host = document.getElementById("toast-host");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s, transform .3s";
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    setTimeout(() => el.remove(), 320);
  }, timeout);
}

/* -------------------------------------------------------------------- api */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (err) {
    payload = { ok: false, error: `HTTP ${response.status}` };
  }
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.code = payload.code || `HTTP_${response.status}`;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/* -------------------------------------------------------------- formatting */
function shortHash(value, head = 10, tail = 8) {
  if (!value) return "--";
  const text = String(value);
  if (text.length <= head + tail + 3) return text;
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

function formatTime(epochSeconds, withDate = true) {
  if (!epochSeconds) return "--";
  const date = new Date(Number(epochSeconds) * 1000);
  const time = date.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (!withDate) return time;
  return `${date.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })} ${time}`;
}

function relativeTime(epochSeconds) {
  if (!epochSeconds) return "--";
  const seconds = Math.floor(Date.now() / 1000 - Number(epochSeconds));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function initials(name) {
  if (!name) return "??";
  return name.split(/\s+/).slice(0, 2).map((part) => part[0].toUpperCase()).join("");
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function statusClass(status) {
  return `s-${String(status || "ABSENT").toUpperCase()}`;
}

/* --------------------------------------------------------------- clipboard */
function copyText(text, label = "Copied") {
  const done = () => toast(`${label} to clipboard`, "ok", 1800);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
    done();
  } catch (err) {
    toast("Copy failed — select the text manually", "warn");
  }
  area.remove();
}

/* Bind every .copy-hash element: click copies its data-copy (or text). */
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-copy]");
  if (target) {
    copyText(target.dataset.copy || target.textContent.trim(), target.dataset.copyLabel || "Copied");
  }
});

/* ----------------------------------------------------------------- QR codes */
/*
 * The QR code itself is generated SERVER-SIDE in Python (qrcode -> SVG) and
 * delivered as a data URI, so there is no client-side encoder to go wrong.
 * Client-side we only need to DECODE, which is what jsQR (vendored) does for
 * the camera on the scan page.
 */

/* Decode a QR image using the vendored jsQR library (camera scanning). */
function decodeQrFromCanvas(canvas) {
  if (typeof jsQR === "undefined") return null;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const { width, height } = canvas;
  const image = ctx.getImageData(0, 0, width, height);
  const result = jsQR(image.data, width, height, { inversionAttempts: "dontInvert" });
  return result ? result.data : null;
}
window.decodeQrFromCanvas = decodeQrFromCanvas;

/* ------------------------------------------------- client-side secp256k1 */
/*
 * Non-custodial mode: the student's browser signs the attendance payload with
 * a key generated locally, so the server never sees the private key.
 * Implemented with BigInt over the same curve as the Python side.
 */
const Secp256k1 = (() => {
  const P = 0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2fn;
  const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
  const Gx = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798n;
  const Gy = 0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8n;

  const mod = (a, m = P) => ((a % m) + m) % m;

  function modInverse(a, m = P) {
    a = mod(a, m);
    let [old_r, r] = [a, m];
    let [old_s, s] = [1n, 0n];
    while (r !== 0n) {
      const q = old_r / r;
      [old_r, r] = [r, old_r - q * r];
      [old_s, s] = [s, old_s - q * s];
    }
    return mod(old_s, m);
  }

  function add(p, q) {
    if (!p) return q;
    if (!q) return p;
    if (p.x === q.x && mod(p.y + q.y) === 0n) return null;
    let lambda;
    if (p.x === q.x && p.y === q.y) {
      lambda = mod(3n * p.x * p.x * modInverse(2n * p.y));
    } else {
      lambda = mod((q.y - p.y) * modInverse(q.x - p.x));
    }
    const x = mod(lambda * lambda - p.x - q.x);
    const y = mod(lambda * (p.x - x) - p.y);
    return { x, y };
  }

  function multiply(k, point) {
    let result = null, addend = point;
    k = mod(k, N);
    while (k > 0n) {
      if (k & 1n) result = add(result, addend);
      addend = add(addend, addend);
      k >>= 1n;
    }
    return result;
  }

  function randomPrivateKey() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    let value = 0n;
    for (const byte of bytes) value = (value << 8n) | BigInt(byte);
    return mod(value, N - 1n) + 1n;
  }

  function publicKeyHex(privateKey) {
    const point = multiply(privateKey, { x: Gx, y: Gy });
    const prefix = point.y & 1n ? "03" : "02";
    return prefix + point.x.toString(16).padStart(64, "0");
  }

  /* SHA-256 in the browser, for building the transaction id. */
  async function sha256Hex(text) {
    const data = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest("SHA-256", data);
    return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  /* RFC 6979-style deterministic nonce is overkill in the browser; we use a
   * random k (secure for a one-shot signature with crypto.getRandomValues). */
  async function sign(privateKey, message) {
    const z = BigInt("0x" + (await sha256Hex(message)));
    while (true) {
      const k = randomPrivateKey();
      const point = multiply(k, { x: Gx, y: Gy });
      if (!point) continue;
      const r = mod(point.x, N);
      if (r === 0n) continue;
      let s = mod(modInverse(k, N) * (z + r * privateKey), N);
      if (s === 0n) continue;
      if (s > N / 2n) s = N - s;
      return r.toString(16).padStart(64, "0") + s.toString(16).padStart(64, "0");
    }
  }

  return { randomPrivateKey, publicKeyHex, sign, sha256Hex, publicKeyFrom: publicKeyHex };
})();

window.Secp256k1 = Secp256k1;

/* --------------------------------------------------------------- device id */
/*
 * A stable, random per-browser identifier, kept in localStorage.
 *
 * The server uses it to detect proxy marking: if one device submits roll
 * numbers for several students in the SAME lecture, that is almost always a
 * student marking their friends present. It is random (never derived from
 * hardware or personal data) and is only ever compared within a single session.
 */
function deviceId() {
  const KEY = "bcoe.attendance.device";
  try {
    let id = localStorage.getItem(KEY);
    if (!id) {
      const bytes = new Uint8Array(16);
      (window.crypto || window.msCrypto).getRandomValues(bytes);
      id = "BCOE-DEV-" + Array.from(bytes)
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("")
        .toUpperCase();
      localStorage.setItem(KEY, id);
    }
    return id;
  } catch (err) {
    // Private mode can block localStorage; fall back to a per-page id.
    return "BCOE-DEV-EPHEMERAL" + Math.random().toString(16).slice(2, 10).toUpperCase();
  }
}
window.deviceId = deviceId;

/* --------------------------------------------------------- polling helper */
function poll(fn, intervalMs) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try {
      await fn();
    } catch (err) {
      console.warn("poll error:", err.message);
    }
    if (!stopped) setTimeout(tick, intervalMs);
  };
  tick();
  return () => { stopped = true; };
}
window.poll = poll;
