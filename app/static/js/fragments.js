// Server re-renders regions (routers/partials.py) instead of hand-patching the DOM.
// refreshFragments() takes no arguments on purpose: every present region is refreshed.

import { noteConnection } from "./core.js";

const FRAGMENTS = [
  { name: "home", targets: ["home-shelves"] },
  { name: "library", targets: ["library-grid"] },
  { name: "storage-summary", targets: ["settings-storage-desc"] },
];

const afterSwapCallbacks = [];

/** Re-run after every swap; callbacks must be idempotent. */
export function onFragmentsSwapped(callback) {
  afterSwapCallbacks.push(callback);
}

// Scroll position lives on the replaced node, so shelves would jump back to the start.
function captureScroll(root) {
  const positions = new Map();
  root.querySelectorAll("[id]").forEach((el) => {
    if (el.scrollLeft > 0) positions.set(el.id, el.scrollLeft);
  });
  return positions;
}

function restoreScroll(positions) {
  positions.forEach((scrollLeft, id) => {
    const el = document.getElementById(id);
    if (el) el.scrollLeft = scrollLeft;
  });
}

// Swaps each <template data-target> into the element with that id. Also used by home/detail.js.
export function swapFragmentHtml(html) {
  const holder = document.createElement("div");
  holder.innerHTML = html;

  let swapped = false;
  holder.querySelectorAll("template[data-target]").forEach((template) => {
    const target = document.getElementById(template.dataset.target);
    if (!target) return;
    const positions = captureScroll(target);
    target.replaceChildren(template.content.cloneNode(true));
    restoreScroll(positions);
    swapped = true;
  });
  return swapped;
}

async function refreshOne({ name, targets }) {
  const present = targets.filter((id) => document.getElementById(id));
  if (!present.length) return false;

  let html;
  try {
    const res = await fetch(`/partials/${name}`);
    noteConnection(true);
    if (!res.ok) return false;
    html = await res.text();
  } catch (err) {
    // Keep the old markup silently, but tell the offline banner: this is the most frequent request.
    noteConnection(false);
    return false;
  }

  return swapFragmentHtml(html);
}

export async function refreshFragments() {
  const results = await Promise.all(FRAGMENTS.map(refreshOne));
  if (results.some(Boolean)) {
    for (const callback of afterSwapCallbacks) callback();
  }
}

/** The ids go up because queue order lives in the browser. */
export async function refreshQueuePanel(ids) {
  if (!document.getElementById("queue-panel-body")) return false;
  let html;
  try {
    const res = await fetch(`/partials/queue?ids=${ids.join(",")}`);
    if (!res.ok) return false;
    html = await res.text();
  } catch (err) {
    return false;
  }
  const swapped = swapFragmentHtml(html);
  if (swapped) {
    for (const callback of afterSwapCallbacks) callback();
  }
  return swapped;
}

/** The Downloads list is large, so it is fetched only when that modal is opened or acted in. */
export async function refreshDownloadsBody() {
  const swapped = await refreshOne({ name: "downloads", targets: ["downloads-body"] });
  if (swapped) {
    for (const callback of afterSwapCallbacks) callback();
  }
  return swapped;
}
