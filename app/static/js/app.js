/* ============================================================================
   BCOE Attendance Chain - front-end behaviour
   ----------------------------------------------------------------------------
   No framework, no build step, no CDN. Loaded on every page; each page adds a
   small <script> block for its own behaviour using the helpers defined here.

   Sections
     1. small utilities   (escaping, formatting, colours)
     2. the API helper    (fetch with sane error handling)
     3. toasts
     4. motion            (reveal on scroll, counting numbers, meters)
     5. theme             (dark / light, remembered)
     6. device identity   (for the proxy-marking guard)
     7. live polling
     8. the QR scanner    (jsQR, phone camera)
     9. shared behaviours (copy-to-clipboard, confirm, tabs)
   ========================================================================== */

/* ------------------------------------------------------------- utilities --- */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Escape text before putting it anywhere near innerHTML. */
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/** ab12cd34...ef56 -- hashes are unreadable at full length. */
function shortHash(value, head = 10, tail = 6) {
  const text = String(value ?? "");
  if (!text) return "--";
  return text.length <= head + tail + 3 ? text : `${text.slice(0, head)}...${text.slice(-tail)}`;
}

function thousands(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-IN") : "--";
}

function formatTime(value) {
  if (!value) return "--";
  const date = new Date(Number(value) * 1000);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDateTime(value) {
  if (!value) return "--";
  return new Date(Number(value) * 1000).toLocaleString([], {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** "just now", "4 min ago", "2 h ago" -- the feed reads better this way. */
function relativeTime(value) {
  if (!value) return "--";
  const seconds = Math.max(0, Date.now() / 1000 - Number(value));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

/**
 * A stable colour for a given string.
 *
 * Used for the little avatars: the same roll number or hash always produces the
 * same hue, so a student's row looks the same on every page. Hash-derived, not
 * identity-derived -- nothing here is personal data.
 */
function hashColour(value) {
  let hash = 0;
  const text = String(value ?? "");
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash << 5) - hash + text.charCodeAt(i);
    hash |= 0;
  }
  const hue = Math.abs(hash) % 360;
  return `linear-gradient(135deg, hsl(${hue} 78% 58%), hsl(${(hue + 48) % 360} 76% 46%))`;
}

/** Two letters that stand for a person, the way a playlist shows artwork. */
function initials(name) {
  const parts = String(name ?? "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "??";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function statusClass(status) {
  switch (String(status ?? "").toUpperCase()) {
    case "PRESENT": case "OK": case "VERIFIED": case "CLOSED": case "SUBMITTED":
      return "ok";
    case "LATE": case "MANUAL": case "OPEN": case "PENDING": case "SIMULATED":
      return "warn";
    case "ABSENT": case "FAILED": case "SHORT": case "INVALID": case "TAMPERED":
      return "danger";
    default:
      return "";
  }
}

/* --------------------------------------------------------------- the API --- */
/**
 * Call the JSON API.
 *
 * Rejections from the ledger carry a stable `code` ("SHARED_DEVICE",
 * "ALREADY_MARKED", ...). We surface both the code and the human message so the
 * UI can show something precise instead of "something went wrong".
 */
async function api(path, options = {}) {
  const config = {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  if (options.body && typeof options.body !== "string") {
    config.body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(path, config);
  } catch (networkError) {
    throw Object.assign(new Error("Could not reach the server."), { code: "OFFLINE" });
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { raw: text }; }
  }

  if (!response.ok) {
    const error = new Error(
      (payload && (payload.error || payload.message)) || `Request failed (${response.status})`,
    );
    error.code = (payload && payload.code) || `HTTP_${response.status}`;
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload ?? {};
}

window.$ = $; window.$$ = $$;
window.api = api;
window.escapeHtml = escapeHtml;
window.shortHash = shortHash;
window.thousands = thousands;
window.formatTime = formatTime;
window.formatDateTime = formatDateTime;
window.relativeTime = relativeTime;
window.hashColour = hashColour;
window.initials = initials;
window.statusClass = statusClass;

/* --------------------------------------------------------------- toasts --- */
/**
 * Show a transient message.
 *   toast("Saved", "ok")  /  toast("Marked late", "warn", 6000)
 */
function toast(message, kind = "info", timeout = 4200) {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  const icons = { ok: "\u2713", danger: "\u26a0", warn: "\u26a0", info: "\u2139" };
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="ico">${icons[kind] || icons.info}</span>
    <span class="msg">${escapeHtml(message)}</span>`;
  host.appendChild(el);

  const close = () => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 320);
  };
  el.addEventListener("click", close);
  setTimeout(close, timeout);
}
window.toast = toast;

/* ------------------------------------------------------------- clipboard --- */
function copyText(text, label = "Copied") {
  const done = () => {
    toast(`${label} to clipboard`, "ok", 1800);
    const chip = document.activeElement && document.activeElement.closest?.(".hash-chip");
    if (chip) {
      chip.classList.add("copied");
      setTimeout(() => chip.classList.remove("copied"), 1400);
    }
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
window.copyText = copyText;

function fallbackCopy(text, done) {
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); done(); }
  catch { toast("Copy failed - select the text manually", "warn"); }
  area.remove();
}

/* Any element carrying data-copy becomes a copy button. */
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-copy]");
  if (target) {
    copyText(target.dataset.copy || target.textContent.trim(), target.dataset.copyLabel || "Copied");
  }
});

/* ---------------------------------------------------------------- motion --- */
const prefersReducedMotion =
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Fade elements in as they scroll into view.
 * Progressive enhancement: if IntersectionObserver is missing, everything is
 * simply shown straight away.
 */
function initReveal(root = document) {
  const items = $$(".reveal:not(.in)", root);
  if (!items.length) return;
  if (prefersReducedMotion || !("IntersectionObserver" in window)) {
    items.forEach((el) => el.classList.add("in"));
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("in");
        observer.unobserve(entry.target);
      }
    });
  }, { rootMargin: "0px 0px -6% 0px", threshold: 0.04 });
  items.forEach((el) => observer.observe(el));
}

/**
 * Count a number up from zero.
 * <span data-count="2867">0</span> becomes 2,867 with tabular figures so the
 * width does not jump while it animates.
 */
function countUp(el) {
  const target = Number(String(el.dataset.count).replace(/[^0-9.\-]/g, ""));
  if (!Number.isFinite(target)) return;
  const decimals = (el.dataset.decimals && Number(el.dataset.decimals)) || 0;
  const duration = Number(el.dataset.duration || 900);
  const format = (value) => Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  });

  if (prefersReducedMotion || duration <= 0) {
    el.textContent = format(target) + (el.dataset.suffix || "");
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - started) / duration);
    // easeOutExpo: fast to almost-there, then settles. Feels like iOS.
    const eased = t === 1 ? 1 : 1 - Math.pow(2, -9 * t);
    el.textContent = format(target * eased) + (el.dataset.suffix || "");
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/**
 * Animate meter bars and rings.
 * A meter is any element with data-pct; bars use --pct, rings use --dash.
 */
function initMeters(root = document) {
  $$("[data-pct]", root).forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    const paint = () => {
      el.style.setProperty("--pct", `${pct}%`);
      if (el.classList.contains("ring")) {
        // Ring dasharray is expressed as a percentage of the circumference.
        el.style.setProperty("--dash", String(pct));
        const label = el.querySelector(".ring-value");
        if (label && !label.dataset.count) label.textContent = `${Math.round(pct)}%`;
      }
    };
    if (prefersReducedMotion) paint();
    else requestAnimationFrame(() => setTimeout(paint, 60));
  });
}

/** A hairline progress bar at the very top of the viewport, iOS-style. */
function initScrollProgress() {
  if (prefersReducedMotion) return;
  const bar = document.createElement("div");
  bar.className = "scroll-progress";
  document.body.appendChild(bar);
  const update = () => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    const pct = max > 0 ? (window.scrollY / max) * 100 : 0;
    bar.style.transform = `scaleX(${Math.max(0, Math.min(1, pct / 100))})`;
  };
  update();
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
}

/**
 * Paint hash avatars.
 * Markup carries only a seed (`data-avatar="BCOE23AI001"`); the colour and the
 * letters are derived here, so the same student looks the same everywhere.
 */
function initAvatars(root = document) {
  $$("[data-avatar]", root).forEach((el) => {
    const seed = el.dataset.avatar || "?";
    if (el.dataset.painted) return;
    el.dataset.painted = "1";
    if (el.style.background) return;          // server already painted it
    el.style.background = hashColour(seed);
    if (!el.textContent.trim()) {
      el.textContent = el.dataset.initials || initials(el.dataset.name || seed.replace(/[^A-Za-z]/g, " "));
    }
  });
}
window.initAvatars = initAvatars;

/* ----------------------------------------------------------------- theme --- */
const THEME_KEY = "bcoe.attendance.theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  $$(".theme-toggle").forEach((btn) => {
    btn.textContent = theme === "light" ? "\u263e" : "\u2600";
    btn.setAttribute("aria-label", theme === "light" ? "Switch to dark theme" : "Switch to light theme");
  });
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* private mode */ }
  const preferred = saved
    || (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  applyTheme(preferred);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".theme-toggle")) return;
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
    applyTheme(next);
  });
}

/* ------------------------------------------------------------ device id --- */
/*
 * A random per-browser identifier used only by the proxy-marking guard: it
 * flags the pattern "one phone, many roll numbers in one session". It is not
 * derived from hardware or personal data, and is only compared within a
 * single lecture.
 */
function deviceId() {
  const KEY = "bcoe.attendance.device";
  try {
    let id = localStorage.getItem(KEY);
    if (!id) {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      id = "BCOE-DEV-" + Array.from(bytes)
        .map((byte) => byte.toString(16).padStart(2, "0")).join("").toUpperCase();
      localStorage.setItem(KEY, id);
    }
    return id;
  } catch {
    return "BCOE-DEV-EPHEMERAL" + Math.random().toString(16).slice(2, 10).toUpperCase();
  }
}
window.deviceId = deviceId;

/* --------------------------------------------------------------- polling --- */
/** Run fn() now and then every intervalMs until the returned stop() is called. */
function poll(fn, intervalMs) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try { await fn(); }
    catch (err) { console.warn("poll error:", err.message); }
    if (!stopped) setTimeout(tick, intervalMs);
  };
  tick();
  return () => { stopped = true; };
}
window.poll = poll;

/* --------------------------------------------------------------- QR codes --- */
/*
 * The QR code itself is generated SERVER-SIDE in Python (qrcode -> SVG) and
 * sent to the browser as a data URI, so there is no client-side encoder to go
 * wrong. On the client we only DECODE, which is what the vendored jsQR does.
 */
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


window.Secp256k1 = Secp256k1;

/* ------------------------------------------------- shared initialisation --- */
function initPage() {
  initTheme();
  initAvatars();
  initReveal();
  initMeters();
  initScrollProgress();
  window.addEventListener("load", () => {
    initAvatars();
    initReveal();
    initMeters();
    $$("[data-count]").forEach(countUp);
  });

  // Animated numbers start when they scroll into view, once each.
  if ("IntersectionObserver" in window && !prefersReducedMotion) {
    const numbers = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { countUp(entry.target); numbers.unobserve(entry.target); }
      });
    }, { threshold: 0.3 });
    $$("[data-count]").forEach((el) => numbers.observe(el));
  } else {
    $$("[data-count]").forEach(countUp);
  }

  // Two-finger-style press feedback on buttons, borrowed from touch UI.
  document.addEventListener("pointerdown", (event) => {
    const btn = event.target.closest(".btn, .nav-item, .subject-tile, .tamper-step");
    if (btn && !prefersReducedMotion) {
      btn.style.transition = "transform 120ms var(--ease)";
      btn.style.transform = "scale(0.975)";
    }
  });
  document.addEventListener("pointerup", (event) => {
    const btn = event.target.closest(".btn, .nav-item, .subject-tile, .tamper-step");
    if (btn) { btn.style.transform = ""; setTimeout(() => { btn.style.transition = ""; }, 130); }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initPage);
} else {
  initPage();
}
