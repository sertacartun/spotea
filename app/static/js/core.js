// Shared vocabulary: escaping, formatting, the JSON helper, toasts, the
// confirm dialog, and the overlay open/close behaviour. Everything here is
// used from at least two other modules — anything used from only one belongs
// in that one.

// What a URL hash means: one of the four tab panels, a channel/playlist
// detail view, a player overlay request, or nothing recognized. Shared by
// home/tabs.js (plain tab switches and its popstate handler) and
// home/detail.js (detail/player routes and its own popstate handler) so the
// two agree on where the line between them is. index.html's inline
// pre-paint script runs before any module loads and can't import this, so it
// necessarily duplicates the same prefix rules in plain JS — this is the one
// place everything after that first paint agrees with it.
const VALID_TABS = ["home", "library", "explore", "settings"];
const PLAYLIST_KINDS = ["favorites", "new-uploads", "recently-played"];
// Detail routes that carry an id. The "yt-" ones are the same panel
// showing something the library doesn't have yet — a recommended YouTube
// playlist, a channel nobody follows, a YouTube Music artist, or one of
// its moods (see app/services/remote_detail.py).
const ID_DETAIL_KINDS = ["yt-playlist", "yt-artist-songs", "yt-artist", "yt-release", "yt-mood"];

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

// Every caller interpolates the result into a double-quoted HTML attribute
// (data-title, aria-label, title, src, ...) as well as into text, so this has
// to be safe in both positions.
//
// This used to be `div.textContent = str; return div.innerHTML`, which is not.
// Serializing a text node escapes `&`, `<` and `>` and nothing else — quotes
// come back through untouched, because inside a text node they need no
// escaping. In attribute position they end the attribute: a video titled
// `" onerror="…` closes data-title and everything after it is parsed as
// further attributes on the same element. Verified against the real
// recVideoCardHtml — three injected handlers fired, and the `<img onerror>`
// one needed no interaction at all, since a broken src fires it on render.
//
// YouTube titles carry quotes constantly (`Why Windows "Clones" Are Bad`), so
// this was also corrupting ordinary markup long before it was a way in.
// Server-rendered templates were never affected: Jinja's autoescape covers
// quotes.
export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Mirrors format_duration() in app/formatting.py (M:SS, or H:MM:SS past an
// hour) — used by both the player's elapsed/duration display and the
// content cards' duration badge.
export function formatDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// Mirrors format_size() in app/formatting.py. A JS twin exists for the same
// reason formatDuration's does — the figure it renders is one the server
// never sees: how much of the device's own storage the offline copies take
// (see offline.js). Kept byte-identical in output so the device total and
// the server total below it don't disagree about how to write "4.1 MB".
export function formatSize(numBytes) {
  if (!numBytes) return "0 MB";
  const megabytes = numBytes / (1024 * 1024);
  if (megabytes >= 1024) return `${(megabytes / 1024).toFixed(2)} GB`;
  return `${megabytes.toFixed(1)} MB`;
}

export function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

/**
 * One JSON request against our own API.
 *
 * Every call site used to write its own try/catch, `res.ok` check, JSON
 * parse and error toast — twenty-five near-identical copies, each free to
 * forget one of the four. Returns a result object rather than throwing or
 * returning bare data because callers genuinely need different parts of it:
 * most want `data`, addChannel needs `status` (409 means "already
 * following", which isn't an error worth toasting), and the pollers just
 * want `ok`.
 *
 * A network failure comes back as { ok: false, status: 0 } rather than
 * raising, so "the server said no" and "the request never arrived" are
 * handled the same way — which is what every call site wants, since neither
 * one leaves the UI with anything to show.
 *
 * `errorMessage` toasts on any failure. Omit it for polls and background
 * refreshes, where a single blip is expected and silence is correct.
 */
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
    if (errorMessage) showToast(errorMessage);
    return { ok: false, status: 0, data: null };
  }

  // 204 (profile/feed deletes) has no body at all, and an error response may
  // carry HTML rather than JSON — neither should turn into a thrown parse
  // error on top of whatever already went wrong.
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
  // Focus the safe choice: these dialogs guard destructive actions, so a
  // reflexive Enter should cancel rather than confirm.
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

// Mobile keyboards resize/scroll the visual viewport independently of the
// layout viewport that `position: fixed` is normally anchored to — open a
// modal-overlay's input and the flex-centered box can drift off toward
// wherever the layout viewport happens to be, instead of the area actually
// on screen. Mirroring the visual viewport's rect onto these vars, kept
// current on every resize/scroll tick, keeps .modal-overlay pinned to what's
// really visible with the keyboard up.
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

// One Escape handler for every overlay, rather than one per overlay. Four
// separate document-level keydown listeners used to be registered (downloads,
// bulk import, switch profile, manage profiles), each re-implementing the
// same "if I'm open, close me" check.
const openableOverlays = [];
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const overlay of openableOverlays) {
    if (!overlay.hidden && !isRequired(overlay)) overlay.hidden = true;
  }
});

/**
 * Whether an overlay is currently refusing to be dismissed.
 *
 * An attribute rather than a wiring-time option, because for the interests
 * overlay it is true only for as long as the first run lasts. That overlay is
 * both the thing a new profile has to get through *and* what Settings' Manage
 * interests opens, and it is wired once at page load — so a flag fixed at
 * wiring time would leave the second one with no way out for the rest of the
 * session. Onboarding clears the attribute when it's done and the same overlay
 * behaves like every other one from then on.
 */
export function isRequired(overlay) {
  return overlay.dataset.required === "true";
}

/**
 * Wire an overlay: one or more triggers open it; the close button, a
 * backdrop click, or Escape close it. Returns { open, close } so a caller
 * can drive it from elsewhere too (the mobile menu opens the profile
 * switcher, for instance) or layer extra behaviour on closing.
 *
 * All three ways out check `data-required` on the overlay first (see
 * isRequired above), so an overlay can refuse to be dismissed for part of a
 * session and behave normally for the rest of it. The interests overlay is
 * the one that needs this: on a brand new profile it is the first run and
 * dismissing it would drop the user into an app with an empty library and
 * empty Explore shelves, which is the state it exists to prevent — but the
 * same element is what Settings' Manage interests opens afterwards, and that
 * one has to close.
 *
 * `close` is returned regardless, since it's what an in-flow action calls.
 */
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

/**
 * Wires a small × button next to a search input: hidden while the input is
 * empty, visible the moment there's anything to clear. Clearing fires a
 * real "input" event rather than just blanking the value, so whatever the
 * input already does on every keystroke (Explore's search, Library's
 * filter) runs exactly as if the text had been deleted by hand — no
 * separate "and also re-run the search" step for each caller to remember.
 */
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
