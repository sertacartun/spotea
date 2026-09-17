// Tab panels live in one document; the "detail" panel and its routes belong to home/detail.js.

import { classifyLocation, tabPath } from "../core.js";

// Fired on every activation, including boot; listeners must check whether they already loaded.
const activationListeners = [];

export function onTabActivated(callback) {
  activationListeners.push(callback);
}

// updateHistory: false for callers whose URL is already right (initial sync, popstate).
export function activate(tabName, { updateHistory = true } = {}) {
  // Offline, only Library works. Guarded here because every tab switch goes through this;
  // "detail" is exempt since openDetail rejects every kind unavailable offline.
  if (tabName !== "library" && tabName !== "detail" && document.body.classList.contains("is-offline")) {
    tabName = "library";
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const isActive = btn.dataset.tab === tabName;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
  });
  // CSS shows panels from data-active-tab, the same attribute index.html's head script sets.
  document.documentElement.dataset.activeTab = tabName;
  // replaceState keeps tab cycling out of back history. The path is the only remembered
  // tab, so a fresh open of "/" starts on Home.
  if (updateHistory && tabName !== "detail") history.replaceState(null, "", tabPath(tabName));
  for (const callback of activationListeners) callback(tabName);
}

export function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  if (!tabButtons.length) return;

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  });

  // Tab switches only replaceState, but channel/playlist links push real history entries.
  window.addEventListener("popstate", () => {
    const info = classifyLocation();
    if (info.type === "tab") activate(info.tab);
    else if (info.type === "unknown") activate("home");
  });

  // The head script already set the initial tab before first paint; don't rewrite the URL.
  activate(document.documentElement.dataset.activeTab || "home", { updateHistory: false });
}
