const VALID_TABS = ["home", "library", "explore", "settings"];
const PLAYLIST_KINDS = ["favorites", "new-uploads", "recently-played", "downloads"];
const ID_DETAIL_KINDS = [
  "yt-playlist",
  "yt-artist-songs",
  "yt-artist",
  "yt-release",
  "yt-mood",
  "user-playlist",
];

export function classifyHash(hash) {
  const [path, query] = hash.split("?");
  const page = query ? Number(query.replace("page=", "")) || 1 : 1;

  const kind = ID_DETAIL_KINDS.find((candidate) => path.startsWith(`${candidate}/`));
  if (kind) {
    return { type: "detail", kind, id: path.slice(kind.length + 1), page };
  }
  if (PLAYLIST_KINDS.includes(path)) {
    return { type: "detail", kind: path, id: null, page };
  }
  if (path.startsWith("player/")) {
    return { type: "player", id: path.slice("player/".length) };
  }
  if (VALID_TABS.includes(path)) {
    return { type: "tab", tab: path };
  }
  return { type: "unknown" };
}

// Must be safe inside double-quoted attributes too, hence quotes are escaped.
export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Mirrors format_duration() in app/formatting.py.
export function formatDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// Mirrors format_size() in app/formatting.py; output must match exactly.
export function formatSize(numBytes) {
  if (!numBytes) return "0 MB";
  const megabytes = numBytes / (1024 * 1024);
  if (megabytes >= 1024) return `${(megabytes / 1024).toFixed(2)} GB`;
  return `${megabytes.toFixed(1)} MB`;
}

// navigator.onLine reads true on a SW-cached reload while offline, so the
// banner runs on whether api() requests actually arrive.
let requestsFailing = false;
let syncConnectionBanner = () => {};

export const CONNECTION_CHANGED = "spotea:connection";

export function noteConnection(reachable) {
  requestsFailing = !reachable;
  // Unconditionally: a banner raised by onLine === false leaves requestsFailing
  // false, so an early return on "no change" would never lower it.
  syncConnectionBanner();
}

// Neither banner signal self-heals, so while offline /health is polled with backoff.
const PROBE_FIRST_DELAY = 3000;
const PROBE_MAX_DELAY = 30000;
// More generous than sw.js's timeout: a false "offline" greys out most of the app.
const PROBE_TIMEOUT_MS = 5000;
let probeTimer = null;
let probeDelay = PROBE_FIRST_DELAY;

/** `cache: "no-store"` and sw.js's API_PREFIXES must both exclude /health —
 *  a probe answered from a cache always says "online". */
async function probeConnection() {
  // Also called directly on visibilitychange; clear so two probe chains never run.
  if (probeTimer !== null) clearTimeout(probeTimer);
  probeTimer = null;
  if (document.hidden) {
    scheduleProbe();
    return;
  }
  // AbortController, not AbortSignal.timeout: on an engine lacking the latter the
  // throw would land in the catch and read as "unreachable" forever.
  const abort = new AbortController();
  const giveUp = setTimeout(() => abort.abort(), PROBE_TIMEOUT_MS);
  try {
    await fetch("/health", { cache: "no-store", signal: abort.signal });
    noteConnection(true);
  } catch {
  // Widened first: noteConnection(false) schedules the next probe itself.
    probeDelay = Math.min(probeDelay * 2, PROBE_MAX_DELAY);
    noteConnection(false);
    scheduleProbe();
  } finally {
    clearTimeout(giveUp);
  }
}

function scheduleProbe() {
  if (probeTimer !== null) return;
  probeTimer = setTimeout(probeConnection, probeDelay);
}

function stopProbing() {
  if (probeTimer !== null) clearTimeout(probeTimer);
  probeTimer = null;
  probeDelay = PROBE_FIRST_DELAY;
}

/**
 * onLine going true is not proof (the cached document reports it while
 * offline), so while the banner is up this polls /health until one lands.
 */
export function watchConnection() {
  const banner = document.getElementById("offline-banner");
  if (!banner) return;
  let wasOffline = null;
  syncConnectionBanner = () => {
    const offline = navigator.onLine === false || requestsFailing;
    banner.hidden = !offline;
    document.body.classList.toggle("is-offline", offline);
    if (offline) scheduleProbe();
    else stopProbing();
    if (offline === wasOffline) return;
    wasOffline = offline;
    document.dispatchEvent(new CustomEvent(CONNECTION_CHANGED, { detail: { offline } }));
  };
  window.addEventListener("online", () => syncConnectionBanner());
  window.addEventListener("offline", () => syncConnectionBanner());
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !banner.hidden) probeConnection();
  });
  syncConnectionBanner();
  // Probe once on load: an unreachable server behind a working phone connection
  // trips neither signal.
  probeConnection();
}

export function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

/** A network failure returns { ok: false, status: 0 } rather than throwing; `errorMessage` toasts on failure. */
export async function api(url, { method = "GET", body, errorMessage } = {}) {
  let res;
  try {
    res = await fetch(url, {
      method,
      ...(body !== undefined && {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    });
  } catch (err) {
    noteConnection(false);
    if (errorMessage) showToast(errorMessage);
    return { ok: false, status: 0, data: null };
  }
  noteConnection(true);

  // 204 has no body and an error may carry HTML; neither should throw a parse error.
  let data = null;
  if (res.status !== 204) {
    data = await res.json().catch(() => null);
  }

  if (!res.ok && errorMessage) {
    showToast(data?.detail || errorMessage);
  }
  return { ok: res.ok, status: res.status, data };
}

export function showToast(message) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  setTimeout(() => {
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}

const CONFIRM_MARKUP = `
  <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modal-message">
    <p id="modal-message"></p>
    <div class="modal-actions">
      <button type="button" id="modal-cancel" class="btn-quiet">Cancel</button>
      <button type="button" id="modal-confirm" class="btn-danger">Confirm</button>
    </div>
  </div>
`;

function ensureModal() {
  let overlay = document.getElementById("modal-overlay");
  if (overlay) return overlay;

  overlay = document.createElement("div");
  overlay.id = "modal-overlay";
  overlay.className = "modal-overlay";
  overlay.hidden = true;
  overlay.innerHTML = CONFIRM_MARKUP;
  document.body.appendChild(overlay);
  return overlay;
}

export function confirmDialog(message, confirmLabel) {
  const overlay = ensureModal();
  const confirmBtn = overlay.querySelector("#modal-confirm");
  const cancelBtn = overlay.querySelector("#modal-cancel");

  overlay.querySelector("#modal-message").textContent = message;
  confirmBtn.textContent = confirmLabel || "Confirm";
  overlay.hidden = false;
  // Focus the safe choice: these dialogs guard destructive actions.
  cancelBtn.focus();

  return new Promise((resolve) => {
    function cleanup(result) {
      overlay.hidden = true;
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    const onConfirm = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onBackdrop = (event) => {
      if (event.target === overlay) cleanup(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") cleanup(false);
    };

    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

// Mobile keyboards move the visual viewport independently of `position: fixed`;
// mirroring its rect into these vars keeps .modal-overlay on the visible area.
if (window.visualViewport) {
  const syncViewportVars = () => {
    const vv = window.visualViewport;
    document.documentElement.style.setProperty("--vvh", `${vv.height}px`);
    document.documentElement.style.setProperty("--vvt", `${vv.offsetTop}px`);
  };
  syncViewportVars();
  window.visualViewport.addEventListener("resize", syncViewportVars);
  window.visualViewport.addEventListener("scroll", syncViewportVars);
}

const openableOverlays = [];
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const overlay of openableOverlays) {
    if (!overlay.hidden && !isRequired(overlay)) overlay.hidden = true;
  }
});

const PROMPT_MARKUP = `
  <div class="modal modal-prompt" role="dialog" aria-modal="true" aria-labelledby="prompt-title">
    <div class="modal-header">
      <h2 id="prompt-title"></h2>
      <button type="button" id="prompt-close" class="modal-close" aria-label="Close">×</button>
    </div>
    <label class="prompt-label" for="prompt-input" id="prompt-message"></label>
    <input type="text" id="prompt-input" class="prompt-input" autocomplete="off" autocapitalize="sentences" />
    <div class="modal-actions">
      <button type="button" id="prompt-cancel" class="btn-quiet">Cancel</button>
      <button type="button" id="prompt-confirm" class="btn-primary">Save</button>
    </div>
  </div>
`;

function ensurePromptModal() {
  let overlay = document.getElementById("prompt-overlay");
  if (overlay) return overlay;

  overlay = document.createElement("div");
  overlay.id = "prompt-overlay";
  overlay.className = "modal-overlay";
  overlay.hidden = true;
  overlay.innerHTML = PROMPT_MARKUP;
  document.body.appendChild(overlay);
  return overlay;
}

/**
 * Resolves to the trimmed string, or null when dismissed. Not window.prompt:
 * installed PWAs can suppress it, making the action silently do nothing.
 */
export function promptDialog(
  message,
  { value = "", confirmLabel = "Save", maxLength = 100, title = "", placeholder = "" } = {}
) {
  const overlay = ensurePromptModal();
  const input = overlay.querySelector("#prompt-input");
  const confirmBtn = overlay.querySelector("#prompt-confirm");
  const cancelBtn = overlay.querySelector("#prompt-cancel");
  const closeBtn = overlay.querySelector("#prompt-close");

  overlay.querySelector("#prompt-title").textContent = title || message;
  const label = overlay.querySelector("#prompt-message");
  label.textContent = title ? message : "";
  label.hidden = !title;
  confirmBtn.textContent = confirmLabel;
  input.value = value;
  input.maxLength = maxLength;
  input.placeholder = placeholder;
  overlay.hidden = false;
  // Next frame: focusing before layout makes iOS scroll to stale geometry, and
  // the dialog visibly slides once the keyboard opens.
  requestAnimationFrame(() => {
    input.focus();
    input.select();
  });

  return new Promise((resolve) => {
    function cleanup(result) {
      overlay.hidden = true;
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      closeBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onBackdrop);
      input.removeEventListener("keydown", onInputKey);
      document.removeEventListener("keydown", onKey);
      // Otherwise the keyboard stays up over a page with nothing to type into.
      input.blur();
      resolve(result);
    }
    const submit = () => {
      const text = input.value.trim();
      cleanup(text || null);
    };
    const onConfirm = () => submit();
    const onCancel = () => cleanup(null);
    const onBackdrop = (event) => {
      if (event.target === overlay) cleanup(null);
    };
    const onInputKey = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    };
    const onKey = (event) => {
      if (event.key === "Escape") cleanup(null);
    };

    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    closeBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onBackdrop);
    input.addEventListener("keydown", onInputKey);
    document.addEventListener("keydown", onKey);
  });
}

export function isRequired(overlay) {
  return overlay.dataset.required === "true";
}

/** Every way out honours `data-required`, which can be toggled at runtime. */
export function setupOverlay(overlayId, closeBtnId, triggerIds = []) {
  const overlay = document.getElementById(overlayId);
  if (!overlay) return null;

  const open = () => {
    overlay.hidden = false;
  };
  const close = () => {
    overlay.hidden = true;
  };
  const dismiss = () => {
    if (!isRequired(overlay)) close();
  };

  for (const triggerId of triggerIds) {
    document.getElementById(triggerId)?.addEventListener("click", open);
  }
  document.getElementById(closeBtnId)?.addEventListener("click", dismiss);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) dismiss();
  });
  openableOverlays.push(overlay);

  return { open, close };
}

/** Clearing dispatches a real "input" event so the input's own handler reruns. */
export function setupSearchClear(inputId, clearBtnId) {
  const input = document.getElementById(inputId);
  const clearBtn = document.getElementById(clearBtnId);
  if (!input || !clearBtn) return;

  const sync = () => {
    clearBtn.hidden = !input.value;
  };
  sync();
  input.addEventListener("input", sync);

  clearBtn.addEventListener("click", () => {
    input.value = "";
    input.dispatchEvent(new Event("input"));
    input.focus();
  });
}
