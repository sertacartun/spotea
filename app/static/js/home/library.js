import { api, setupSearchClear, showToast } from "../core.js";
import { onFragmentsSwapped, refreshFragments } from "../fragments.js";
import { wireScrollers } from "./scrollers.js";
import { openDetail } from "./detail.js";

// A history scan takes minutes, so this only notices it ending; runs only while such a card exists.
const PREPARING_POLL_MS = 5000;

let preparingTimer = null;

function preparingArtistIds() {
  return [...document.querySelectorAll("#library-grid [data-preparing]")].map(
    (card) => card.dataset.detailId
  );
}

async function checkPreparing() {
  preparingTimer = null;
  const showing = preparingArtistIds();
  if (!showing.length) return;

  const { ok, data } = await api("/artists/syncing");
  if (ok) {
    const stillRunning = new Set(data.map(String));
    // Swap the grid only when a card's scan has actually finished.
    if (showing.some((artistId) => !stillRunning.has(artistId))) await refreshFragments();
  }
  schedulePreparingCheck();
}

function schedulePreparingCheck() {
  if (preparingTimer || !preparingArtistIds().length) return;
  preparingTimer = setTimeout(checkPreparing, PREPARING_POLL_MS);
}

/** Clears Library's "Fetching releases…" cards once their background sync ends. */
export function setupPreparingArtists() {
  schedulePreparingCheck();
  onFragmentsSwapped(schedulePreparingCheck);
}

// The minimum stops a fast load flickering; the maximum stops one stalled image trapping the app.
const SPLASH_MIN_VISIBLE_MS = 400;
const SPLASH_MAX_WAIT_MS = 4000;

/** Dismisses the boot splash on `load`, not DOMContentLoaded, which fires before images paint. */
export function setupSplash() {
  const splash = document.getElementById("app-splash");
  if (!splash) return;

  const shownAt = performance.now();
  let dismissed = false;

  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    const wait = Math.max(0, SPLASH_MIN_VISIBLE_MS - (performance.now() - shownAt));
    setTimeout(() => {
      splash.classList.add("app-splash-hide");
      // [hidden] removes it from layout and tab order; the class only fades. If transitionend
      // never fires, app-splash-hide's pointer-events: none keeps it out of the way.
      splash.addEventListener("transitionend", () => { splash.hidden = true; }, { once: true });
    }, wait);
  };

  if (document.readyState === "complete") dismiss();
  else window.addEventListener("load", dismiss, { once: true });
  setTimeout(dismiss, SPLASH_MAX_WAIT_MS);
}

// Delegated: the chips and see-more links are replaced on every fragment refresh.
export function setupHomeArtists() {
  document.getElementById("tab-home")?.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    const chip = event.target.closest(".channel-chip");
    if (chip) {
      openDetail("yt-artist", chip.dataset.browseId);
      return;
    }

    const seeMore = event.target.closest(".shelf-see-more[data-detail-kind]");
    if (seeMore) {
      event.preventDefault();
      openDetail(seeMore.dataset.detailKind, null);
    }
  });
}

// Delegated: #library-grid's contents are replaced on every fragment refresh.
export function setupLibraryArtistGrid() {
  document.getElementById("tab-library")?.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    const card = event.target.closest(".channel-card");
    // Some cards (e.g. "New playlist") wear the class only for shape; openDetail(undefined) blanks the tabs.
    if (!card?.dataset.detailKind) return;
    event.preventDefault();
    openDetail(card.dataset.detailKind, card.dataset.detailId || null);
  });
}

// Cards are re-queried each keystroke: the grid is replaced on every fragment refresh.
function applyLibraryFilter() {
  const input = document.getElementById("library-search-input");
  const grid = document.querySelector("#library-grid .channel-grid");
  const emptyState = document.getElementById("channel-search-empty");
  if (!input || !grid) return;

  const query = input.value.trim().toLowerCase();
  let visibleCount = 0;

  grid.querySelectorAll(".channel-card").forEach((card) => {
    const title = card.querySelector(".channel-card-title")?.textContent.toLowerCase() ?? "";
    const matches = !query || title.includes(query);
    card.hidden = !matches;
    if (matches) visibleCount++;
  });

  if (emptyState) emptyState.hidden = visibleCount > 0;
}

export function setupLibrarySearch() {
  const input = document.getElementById("library-search-input");
  if (!input) return;
  input.addEventListener("input", applyLibraryFilter);
  setupSearchClear("library-search-input", "library-search-clear");
  onFragmentsSwapped(applyLibraryFilter);
}

export function setupHorizontalScrollers() {
  wireScrollers();
  // Registered here, not inside wireScrollers, which would add a callback on every swap.
  onFragmentsSwapped(wireScrollers);
}

// The overlay is the feedback because refresh-artists-btn is hidden under the mobile-menu breakpoint.
async function refreshArtists(alsoRefresh) {
  const overlay = document.getElementById("refresh-overlay");
  const btn = document.getElementById("refresh-artists-btn");
  if (overlay) overlay.hidden = false;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("is-spinning");
  }

  const [{ ok }] = await Promise.all([
    api("/artists/refresh", { method: "POST" }),
    alsoRefresh ? alsoRefresh() : Promise.resolve(),
  ]);

  // Always re-render: new_content_count misses rewritten release snapshots and
  // content other triggers (background job, other tabs) added since page load.
  if (ok) await refreshFragments();
  else showToast("Could not refresh feeds");

  if (overlay) overlay.hidden = true;
  if (btn) {
    btn.disabled = false;
    btn.classList.remove("is-spinning");
  }
}

// `alsoRefresh` is injected to avoid a library.js <-> explore.js import cycle.
export function setupRefreshButton(alsoRefresh) {
  document
    .getElementById("refresh-artists-btn")
    ?.addEventListener("click", () => refreshArtists(alsoRefresh));
}

export function setupMobileMenu(alsoRefresh) {
  const btn = document.getElementById("mobile-menu-btn");
  const menu = document.getElementById("mobile-menu");
  if (!btn || !menu) return;

  const setOpen = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };

  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });

  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== btn) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setOpen(false);
  });

  document.getElementById("mobile-menu-refresh")?.addEventListener("click", () => {
    setOpen(false);
    refreshArtists(alsoRefresh);
  });
}
