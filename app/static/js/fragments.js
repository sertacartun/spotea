// Server re-renders regions (routers/partials.py) instead of hand-patching the DOM.
// refreshFragments() takes no arguments on purpose: every present region is refreshed.

import { CONNECTION_CHANGED, noteConnection } from "./core.js";

const FRAGMENTS = [
  { name: "home", targets: ["home-shelves"] },
  { name: "library", targets: ["library-grid"] },
  { name: "about", targets: ["settings-about"] },
  {
    name: "storage-summary",
    targets: ["settings-downloads-desc", "settings-downloads-actions", "settings-cache-desc", "settings-cache-actions"],
  },
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

// Several refreshes are routinely in flight at once — a track starting (home/overlay.js posts
// /played then refreshes), Library's 5-second "preparing" poll, a favourite, an unfollow. Their
// responses can arrive in any order, and a response is only ever as fresh as the moment the
// server built it. Without this, an answer prepared before a change and delivered after one
// prepared later would win, putting back exactly what the newer answer had removed — the
// unfollowed artist reappearing on Home, with a broken avatar once the orphan sweep had
// deleted the file behind it.
const generations = new Map();

async function refreshOne({ name, targets }) {
  const present = targets.filter((id) => document.getElementById(id));
  if (!present.length) return false;

  const generation = (generations.get(name) ?? 0) + 1;
  generations.set(name, generation);

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

  // A newer refresh of this region started while this one was in flight; that one's answer is
  // the current one, whichever arrives first.
  if (generations.get(name) !== generation) return false;

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

  const generation = (generations.get("queue") ?? 0) + 1;
  generations.set("queue", generation);

  let html;
  try {
    const res = await fetch(`/partials/queue?ids=${ids.join(",")}`);
    if (!res.ok) return false;
    html = await res.text();
  } catch (err) {
    return false;
  }
  // Reordering twice quickly sends two id lists; the later one describes the queue.
  if (generations.get("queue") !== generation) return false;

  const swapped = swapFragmentHtml(html);
  if (swapped) {
    for (const callback of afterSwapCallbacks) callback();
  }
  return swapped;
}

// A document is only as current as the moment the server rendered it, and this one can be very
// old: sw.js serves its cached shell whenever the network takes longer than NETWORK_TIMEOUT_MS
// or is gone, and that copy is only replaced by the next *successful full navigation* — which
// an SPA hardly ever makes. So a phone waking on a slow tailnet link can paint a Home from days
// ago. Anything older than this gets one refresh on boot to catch up.
const STALE_DOCUMENT_MS = 30_000;

/** Fragments are otherwise only refreshed by an action; these are the two cases with no action. */
export function installStalenessRefresh() {
  const renderedAt = Number(document.body.dataset.renderedAt);
  if (!Number.isFinite(renderedAt) || Date.now() - renderedAt > STALE_DOCUMENT_MS) {
    refreshFragments();
  }

  // Coming back from offline is the other one: core.js's only listener for this handles going
  // offline, so until now nothing re-read the shelves on the way back.
  let wasOffline = false;
  document.addEventListener(CONNECTION_CHANGED, (event) => {
    const offline = Boolean(event.detail?.offline);
    // Not on the first announcement, which fires on every boot and would double every load.
    if (wasOffline && !offline) refreshFragments();
    wasOffline = offline;
  });
}
