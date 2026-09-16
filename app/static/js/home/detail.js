import { unfollowArtist } from "../content-actions.js";
import { classifyHash, noteConnection, showToast } from "../core.js";
import { refreshFragments, swapFragmentHtml } from "../fragments.js";
import { applyAmbientTint } from "./ambient.js";
import { deviceTrackIds, renderDownloadsPanel } from "./device.js";
import { OPEN_ARTIST, openPlayer } from "./overlay.js";
import { PLAYLIST_CHANGED, PLAYLIST_DELETED } from "./playlists.js";
import { QUEUE_CHANGED, isShuffled, loadQueue, queueSource, setQueue, toggleShuffle } from "./queue.js";
import { wireScrollers } from "./scrollers.js";
import { ARTIST_FOLLOWED, followArtist, playRemoteList, playRemoteVideo } from "./remote.js";
import { activate } from "./tabs.js";

const REMOTE_KINDS = ["yt-playlist", "yt-artist", "yt-artist-songs", "yt-release", "yt-mood"];
const isRemoteKind = (kind) => REMOTE_KINDS.includes(kind);

// "downloads" is drawn from IndexedDB by home/device.js, never from the network.
const isDeviceKind = (kind) => kind === "downloads";

// Remote fragments cached per exact URL: each costs a live YouTube Music read and
// nothing here mutates one. Local kinds always refetch. Map order = oldest first.
const REMOTE_CACHE_LIMIT = 20;
const remoteFragmentCache = new Map();

// Also drives which tab button stays selected while the panel is open (style.css).
const detailHome = (kind) => (isRemoteKind(kind) ? "explore" : "library");

// `pushed`: whether a history entry sits behind this view; if not (deep link,
// reload), history.back() would leave the app. { kind, id, page, pushed } | null.
let current = null;

const hasId = (kind) => isRemoteKind(kind) || kind === "user-playlist";

function detailUrl(kind, id, page, title) {
  const base = hasId(kind) ? `/partials/detail/${kind}/${id}` : `/partials/detail/playlist/${kind}`;
  const params = new URLSearchParams();
  if (page > 1) params.set("page", page);
  // Display-only optimization for yt-mood: saves one server-side lookup.
  if (title) params.set("title", title);
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function hashFor(kind, id, page) {
  const path = hasId(kind) ? `${kind}/${id}` : kind;
  return page > 1 ? `#${path}?page=${page}` : `#${path}`;
}

function showLoading() {
  const panel = document.getElementById("detail-panel");
  if (panel) {
    panel.innerHTML = `<div class="detail-loading"><span class="spinner spinner-lg" role="status" aria-label="Loading"></span></div>`;
  }
}

function cacheRemoteFragment(url, html) {
  remoteFragmentCache.set(url, html);
  if (remoteFragmentCache.size > REMOTE_CACHE_LIMIT) {
    remoteFragmentCache.delete(remoteFragmentCache.keys().next().value);
  }
}

/** "Failed and already told the user", distinct from null (multi-track release). */
const FAILED = Symbol("release-failed");

const singleReleaseCache = new Map();

/**
 * Returns the track for a one-track release, null for a longer one (priming
 * remoteFragmentCache for the openDetail that follows), or FAILED.
 */
async function resolveRelease(browseId) {
  const url = detailUrl("yt-release", browseId, 1);
  if (singleReleaseCache.has(url)) return singleReleaseCache.get(url);
  if (remoteFragmentCache.has(url)) return null;

  let res;
  try {
    res = await fetch(url);
  } catch (err) {
    // These are plain fetch()es, not api(), so the banner must be told here.
    noteConnection(false);
    showToast("Could not load this page");
    return FAILED;
  }
  noteConnection(true);
  if (!res.ok) {
    showToast(res.status === 404 ? "That's gone." : "Could not load this page");
    return FAILED;
  }

  if (res.headers.get("content-type")?.includes("application/json")) {
    const track = await res.json();
    singleReleaseCache.set(url, track);
    return track;
  }
  cacheRemoteFragment(url, await res.text());
  return null;
}

/**
 * `replace` is for syncing to a URL that's already current (boot, popstate,
 * pagination); a fresh open pushes a history entry.
 */
export async function openDetail(kind, id, { page = 1, replace = false, title } = {}) {
  if (!isDeviceKind(kind) && document.body.classList.contains("is-offline")) {
    showToast("You're offline — only your Downloads are available");
    activate("library");
    return;
  }

  // A one-track release plays instead of opening. Resolved before the history
  // push, so a single never leaves a panel entry to go "back" to.
  if (kind === "yt-release") {
    const single = await resolveRelease(id);
    if (single === FAILED) return;
    if (single) {
      await playRemoteVideo(single);
      return;
    }
  }

  // Pagination always replaces, so it must keep the original open's `pushed`.
  const isSameTarget = current && current.kind === kind && String(current.id) === String(id);
  const pushed = isSameTarget ? current.pushed : !replace;
  current = { kind, id, page, pushed };
  document.documentElement.dataset.detailHome = detailHome(kind);
  activate("detail", { updateHistory: false });
  const hash = hashFor(kind, id, page);
  if (replace) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);

  if (isDeviceKind(kind)) {
    await renderDownloadsPanel();
    afterPanelSwap();
    return;
  }

  const url = detailUrl(kind, id, page, title);
  const cached = isRemoteKind(kind) ? remoteFragmentCache.get(url) : undefined;
  if (cached !== undefined) {
    swapFragmentHtml(cached);
    afterPanelSwap();
    return;
  }

  showLoading();

  let res;
  try {
    res = await fetch(url);
  } catch (err) {
    noteConnection(false);
    showToast("Could not load this page");
    return;
  }
  noteConnection(true);
  if (!res.ok) {
    showToast(res.status === 404 ? "That's gone." : "Could not load this page");
    return;
  }
  const html = await res.text();
  if (isRemoteKind(kind)) cacheRemoteFragment(url, html);
  swapFragmentHtml(html);
  afterPanelSwap();
}

/** Shared by both swap paths (cache hit and fetch) so they can't drift. */
function afterPanelSwap() {
  syncShuffleButton();
  wireScrollers();
  applyAmbientTint();
}

function currentSource() {
  return current && { kind: current.kind, id: current.id ?? null };
}

function isSameSource(a, b) {
  return Boolean(a && b && a.kind === b.kind && String(a.id) === String(b.id));
}

function syncShuffleButton() {
  const btn = document.getElementById("detail-shuffle");
  if (!btn) return;
  btn.classList.toggle("is-on", isShuffled());
  btn.setAttribute("aria-pressed", String(isShuffled()));
}

/** Fills the queue from the whole list (every page, not just the rows on screen). */
async function playAll(button) {
  const source = currentSource();
  if (!source) return;
  if (isRemoteKind(source.kind)) {
    playRemoteList(source, { button });
    return;
  }
  // Device list: ids are already here, and offline the request wouldn't arrive.
  if (isDeviceKind(source.kind)) {
    const ids = deviceTrackIds();
    if (!ids.length) {
      showToast("Nothing to play here");
      return;
    }
    const startId = setQueue(source, ids);
    openPlayer(startId ?? ids[0]);
    return;
  }
  // Guards a double press building a second queue and restarting playback.
  button.disabled = true;
  try {
    const startId = await loadQueue(source);
    if (startId == null) {
      showToast("Nothing to play here");
      return;
    }
    openPlayer(startId);
  } finally {
    button.disabled = false;
  }
}

// Falls back to switching tabs when this view was never pushed, otherwise
// history.back() would pop past the app entirely.
function closeDetail() {
  const pushed = current?.pushed;
  const home = detailHome(current?.kind);
  current = null;
  if (pushed) history.back();
  else activate(home);
}

async function handleUnfollow(artistId, button) {
  button.disabled = true;
  const ok = await unfollowArtist(artistId);
  if (!ok) {
    button.disabled = false;
    return;
  }
  closeDetail();
  refreshFragments();
}

function pageFromHref(href) {
  const match = href.match(/[?&]page=(\d+)/);
  return match ? Number(match[1]) : 1;
}

/** Marked busy: a single plays without changing the page, so the tap would
 *  otherwise look ignored. */
function openReleaseCard(card) {
  card.classList.add("is-loading");
  openDetail("yt-release", card.dataset.releaseId).finally(() => {
    card.classList.remove("is-loading");
  });
}

export function setupDetailPanel() {
  const panel = document.getElementById("detail-panel");
  if (!panel) return;

  // Home's "New releases" shelf holds release cards outside #detail-panel.
  // Bound here because overlay.js can't import this module (circular).
  document.getElementById("tab-home")?.addEventListener("click", (event) => {
    const releaseCard = event.target.closest(".release-card");
    if (releaseCard) openReleaseCard(releaseCard);
  });

  // Delegated: swapFragmentHtml replaces #detail-panel's children on every open.
  panel.addEventListener("click", (event) => {
    if (event.target.closest("#detail-back-btn")) {
      closeDetail();
      return;
    }

    const unfollowBtn = event.target.closest("#unfollow-artist-btn");
    if (unfollowBtn) {
      handleUnfollow(unfollowBtn.dataset.artistId, unfollowBtn);
      return;
    }

    const followBtn = event.target.closest("#follow-artist-btn");
    if (followBtn) {
      followArtist(followBtn.dataset.channelUrl, followBtn);
      return;
    }

    const playAllBtn = event.target.closest("#detail-play-all");
    if (playAllBtn) {
      playAll(playAllBtn);
      return;
    }

    // Shuffle is a toggle, not a play button: it never starts or interrupts playback.
    if (event.target.closest("#detail-shuffle")) {
      toggleShuffle();
      return;
    }

    const bioToggle = event.target.closest("#artist-bio-toggle");
    if (bioToggle) {
      const expanded = bioToggle.getAttribute("aria-expanded") === "true";
      bioToggle.setAttribute("aria-expanded", String(!expanded));
      bioToggle.textContent = expanded ? "More" : "Less";
      document.getElementById("artist-bio")?.classList.toggle("is-expanded", !expanded);
      return;
    }

    const releaseCard = event.target.closest(".release-card");
    if (releaseCard) {
      openReleaseCard(releaseCard);
      return;
    }

    const artistCard = event.target.closest(".shelf-channel-card");
    if (artistCard) {
      openDetail("yt-artist", artistCard.dataset.channelId);
      return;
    }

    // A card, not a row: artist videos carry no duration (music.ArtistProfile.videos).
    const videoCard = event.target.closest(".rec-card[data-video-id]");
    if (videoCard) {
      playRemoteVideo(videoCard.dataset, videoCard.querySelector(".rec-play"));
      return;
    }

    const playlistCard = event.target.closest(".rec-card[data-playlist-id]");
    if (playlistCard) {
      openDetail("yt-playlist", playlistCard.dataset.playlistId);
      return;
    }

    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    const seeAll = event.target.closest(".artist-see-all");
    if (seeAll && current) {
      event.preventDefault();
      openDetail("yt-artist-songs", current.id);
      return;
    }

    const pageLink = event.target.closest(".pagination-btn:not(.is-disabled)");
    if (pageLink && current) {
      event.preventDefault();
      openDetail(current.kind, current.id, { page: pageFromHref(pageLink.getAttribute("href")), replace: true });
      return;
    }

    const trackLink = event.target.closest(".track-row .track-link");
    if (trackLink) {
      event.preventDefault();
      const row = trackLink.closest(".track-row");
      const source = currentSource();

      if (row.dataset.videoId) {
        if (source) playRemoteList(source, { startVideoId: row.dataset.videoId });
        return;
      }

      const contentId = row.dataset.contentId;
      // Checked before openPlayer, which clears a queue the clicked track isn't
      // part of (queue.js's noteCurrent) and would make this always a miss.
      const alreadyQueued = isSameSource(queueSource(), source);
      // Not awaited: the queue is only needed when this track ends.
      openPlayer(contentId);
      if (alreadyQueued || !source) return;
      if (isDeviceKind(source.kind)) setQueue(source, deviceTrackIds(), { startId: contentId });
      else loadQueue(source, { startId: contentId });
    }
  });

  document.addEventListener(QUEUE_CHANGED, syncShuffleButton);

  // Re-opened rather than patching the DOM: row, count and pagination all come
  // from the server.
  document.addEventListener(PLAYLIST_CHANGED, (event) => {
    if (current?.kind !== "user-playlist") return;
    if (String(current.id) !== String(event.detail?.playlistId)) return;
    openDetail(current.kind, current.id, { page: current.page, replace: true });
  });

  document.addEventListener(PLAYLIST_DELETED, (event) => {
    if (current?.kind !== "user-playlist") return;
    if (String(current.id) !== String(event.detail?.playlistId)) return;
    closeDetail();
  });

  document.addEventListener(ARTIST_FOLLOWED, (event) => {
    // The event carries the new row id, not the browse id cache entries are keyed on.
    remoteFragmentCache.clear();
  });

  document.addEventListener(OPEN_ARTIST, (event) => {
    openDetail("yt-artist", event.detail.pageId);
  });

  window.addEventListener("popstate", () => {
    const info = classifyHash(location.hash.slice(1));
    if (info.type === "detail") openDetail(info.kind, info.id, { page: info.page, replace: true });
    else if (info.type === "player") openPlayer(info.id);
    else current = null;
  });
}

// Called once at boot, once setupPlayerOverlay can receive an openPlayer call.
export function handleInitialRoute() {
  const info = classifyHash(location.hash.slice(1));
  if (info.type === "detail") openDetail(info.kind, info.id, { page: info.page, replace: true });
  else if (info.type === "player") openPlayer(info.id);
}
