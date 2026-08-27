// Home/Library/Explore/Settings are panels in one document, not four pages.
// The fifth panel, "detail" (a channel or a pinned playlist), is driven by
// home/detail.js instead — it has its own popstate listener for that; this
// one only acts when the hash resolves to one of the four tabs above, or to
// neither a tab nor a detail/player route.

import { classifyHash } from "../core.js";

// Panels that only fill themselves in once they're actually looked at —
// currently just Explore's recommendations, which can cost a live YouTube
// round trip and so shouldn't be paid for by someone who never opens the
// tab. Registered callbacks fire on every activation, including the boot
// one, so a listener has to decide for itself whether it's already loaded.
const activationListeners = [];

export function onTabActivated(callback) {
  activationListeners.push(callback);
}

// Exported so home/detail.js can switch to the "detail" panel the same way a
// tab click does — but it never carries "detail" itself into the URL (the URL
// for a detail view is a channel/playlist route detail.js owns, not this).
// Callers that already know the URL is correct (initial sync, popstate) pass
// updateHistory: false so this doesn't stomp it with a redundant replaceState.
export function activate(tabName, { updateHistory = true } = {}) {
  // With no connection there is exactly one tab worth being on: Library,
  // whose Downloads tile is the only thing in the app that does not need the
  // server (see home/device.js). Guarded here rather than at each caller
  // because this is the single door every tab switch goes through — a click,
  // a hash, a popstate, the mobile menu — and the controls CSS puts out of
  // reach are only the ones that are on screen. "detail" is exempt: the one
  // detail view reachable offline is the device's own, and openDetail turns
  // away every other kind before it gets here.
  if (tabName !== "library" && tabName !== "detail" && document.body.classList.contains("is-offline")) {
    tabName = "library";
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const isActive = btn.dataset.tab === tabName;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
  });
  // Panel visibility itself is driven by the data-active-tab attribute via
  // CSS (see style.css) — this is what index.html's inline head script also
  // sets, so both the first paint and later clicks go through the same path.
  document.documentElement.dataset.activeTab = tabName;
  // replaceState (not pushState) so cycling through tabs doesn't spam the
  // back-button history — the URL just needs to be right for a refresh. This
  // is also the only place the current tab is remembered: there used to be a
  // localStorage copy behind it, which meant opening the app fresh (a PWA
  // launch, or plain "/") reopened whatever tab was last used — including
  // Settings, right after an action taken from Settings. The hash already
  // survives a reload, and a fresh open with no hash is meant to start on
  // Home.
  if (updateHistory && tabName !== "detail") history.replaceState(null, "", `#${tabName}`);
  for (const callback of activationListeners) callback(tabName);
}

export function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  if (!tabButtons.length) return;

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  });

  // Needed because most tab switches only replaceState (see above), but
  // leaving Home via a channel/playlist link is a real navigation — without
  // this, pressing back would update the URL's hash without the visible
  // panel following it. Detail/player routes are home/detail.js's own
  // popstate listener's job, not this one's.
  window.addEventListener("popstate", () => {
    const info = classifyHash(location.hash.slice(1));
    if (info.type === "tab") activate(info.tab);
    else if (info.type === "unknown") activate("home");
  });

  // The inline head script already resolved the correct initial tab (the URL
  // hash, or Home) and set it on <html> before first
  // paint — this just syncs the button/UI state to match, without rewriting
  // a URL that's already right (and, for a detail route, isn't this
  // module's to rewrite at all).
  activate(document.documentElement.dataset.activeTab || "home", { updateHistory: false });
}
