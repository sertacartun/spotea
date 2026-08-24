// The Library tab's channel grid and search, Home's channel chips, the
// drag-to-scroll shelves, the collapsed mobile menu, and the manual feed
// refresh those last two both trigger.

import { api, setupSearchClear, showToast } from "../core.js";
import { onFragmentsSwapped, refreshFragments } from "../fragments.js";
import { wireScrollers } from "./scrollers.js";
import { openDetail } from "./detail.js";

// How often a Library card that says "Fetching releases…" checks whether
// that is still true. A history scan is minutes long, so this is about
// noticing it *ended*, not about tracking its progress — and it only runs at
// all while such a card is on the page.
const PREPARING_POLL_MS = 5000;

let preparingTimer = null;

/** The feed ids Library is currently showing as still being fetched. */
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
    // Only when the grid and the server disagree — a card claiming to be
    // preparing for a scan that has finished. Re-rendering on every tick
    // regardless would be a needless swap of the whole grid every five
    // seconds, most of them changing nothing.
    if (showing.some((artistId) => !stillRunning.has(artistId))) await refreshFragments();
  }
  schedulePreparingCheck();
}

function schedulePreparingCheck() {
  if (preparingTimer || !preparingArtistIds().length) return;
  preparingTimer = setTimeout(checkPreparing, PREPARING_POLL_MS);
}

/**
 * Keeps Library's "Fetching releases…" cards honest.
 *
 * A newly followed channel gets a card as soon as its feed row exists —
 * POST /feeds answers there and leaves the rest to a background job (see
 * services/initial_sync.py): the catalogue snapshot, then
 * the full upload history, minutes on a large channel. That wait used to be
 * held in front of whoever added it (the onboarding wizard sat on a loading
 * screen for it); now it lives on the card of the channel it belongs to,
 * where it can be ignored, and this is what takes it back off again — the
 * refresh below is also what puts the channel's videos onto Home, since the
 * card can now appear before there are any.
 */
export function setupPreparingArtists() {
  schedulePreparingCheck();
  // A fragment swap can bring in cards that weren't preparing before (the
  // onboarding wizard's own refresh, on the way out, is the usual one).
  onFragmentsSwapped(schedulePreparingCheck);
}

// Delegated from the panel rather than the chip row/see-more links
// themselves: both live inside the Home fragment and are replaced wholesale
// on every refresh, which would take a directly-bound listener with them.
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

// Same idea for the Library grid's pinned playlist tiles and per-channel
// cards — delegated from the panel (rather than bound per-card) because
// #library-grid's contents are replaced wholesale on every fragment refresh.
export function setupLibraryArtistGrid() {
  document.getElementById("tab-library")?.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    const card = event.target.closest(".channel-card");
    // A card with no kind has nothing to open — it wears this class for the
    // shape, not to navigate. "New playlist" is the first of those (see
    // _library_grid.html); without this it called openDetail(undefined),
    // which swapped in an empty detail panel *over* Library and left every
    // tab panel display: none behind it.
    if (!card?.dataset.detailKind) return;
    event.preventDefault();
    openDetail(card.dataset.detailKind, card.dataset.detailId || null);
  });
}

// Every card is already server-rendered in the DOM, so filtering is just a
// show/hide over what's there — no round trip needed.
//
// The cards are looked up on each keystroke rather than captured once: the
// grid is inside the Library fragment and is replaced on every refresh, so a
// captured list would go stale (and the grid is a handful of nodes, so
// re-querying costs nothing).
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
  // A refreshed grid comes back unfiltered, so whatever is in the box has to
  // be applied again.
  onFragmentsSwapped(applyLibraryFilter);
}

export function setupHorizontalScrollers() {
  wireScrollers();
  // Rows inside the Home fragment are replaced on every refresh, so newly
  // swapped-in ones need wiring too. Registered here, once, rather than from
  // inside wireScrollers — doing it there would add another callback on every
  // swap.
  onFragmentsSwapped(wireScrollers);
}


// Feeds are also kept fresh by a server-side background job on a schedule set
// in Settings (see routers/settings.py) — this is just for "I want it now".
// The overlay (rather than just the button's own spin state) is the feedback
// here because refresh-artists-btn itself is hidden under the mobile-menu
// breakpoint (see style.css); the overlay covers that entry point too.
async function refreshArtists(alsoRefresh) {
  const overlay = document.getElementById("refresh-overlay");
  const btn = document.getElementById("refresh-artists-btn");
  if (overlay) overlay.hidden = false;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("is-spinning");
  }

  // Explore's recommendations are rebuilt alongside the feeds rather than
  // having a refresh control of their own — one button means "go and look at
  // everything again". Run together, since both are slow and independent.
  const [{ ok }] = await Promise.all([
    api("/artists/refresh", { method: "POST" }),
    alsoRefresh ? alsoRefresh() : Promise.resolve(),
  ]);

  // Always re-render, regardless of new_content_count: that figure only
  // counts rows this exact call inserted, not the release snapshots the same
  // sync rewrote (which is what both "New releases" surfaces read), nor
  // content some other trigger (the background refresh job, another tab,
  // another device) had already added since this page was rendered. This used to be a full page reload, which meant saving and
  // restoring playback around it; re-rendering the shelves in place leaves
  // the player alone entirely.
  if (ok) await refreshFragments();
  else showToast("Could not refresh feeds");

  if (overlay) overlay.hidden = true;
  if (btn) {
    btn.disabled = false;
    btn.classList.remove("is-spinning");
  }
}

// `alsoRefresh` is injected rather than imported (pages/index.js passes
// home/explore.js's refreshRecommendations) — importing it here would make
// library.js and explore.js import each other. Same arrangement as
// setupMobileMenu's
// openProfileSwitcher below.
export function setupRefreshButton(alsoRefresh) {
  document
    .getElementById("refresh-artists-btn")
    ?.addEventListener("click", () => refreshArtists(alsoRefresh));
}

// Below the mobile-menu-btn breakpoint (see style.css), the refresh/logout
// row collapses into this single hamburger dropdown instead — same
// underlying actions, just consolidated so the topbar doesn't have to fit
// several separate controls on one narrow line.
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
