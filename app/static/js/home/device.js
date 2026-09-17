// What this device holds (IndexedDB via ../offline.js): kept lists, their sync, and the offline lock.

import {
  CONNECTION_CHANGED,
  api,
  confirmDialog,
  escapeHtml,
  formatDuration,
  formatSize,
  showToast,
} from "../core.js";
import { onFragmentsSwapped } from "../fragments.js";
import {
  deleteTrack,
  isSupported as deviceStorageSupported,
  keepList,
  keptLists,
  listSaved,
  openCoverUrl,
  reconcileList,
  requestPersistence,
  saveTrack,
  savedTrackIds,
} from "../offline.js";
import { activeAudio, onPlayerEvent } from "../player.js";
import { materializeRemoteRows } from "./remote.js";
import { activate } from "./tabs.js";

// Rows the open Downloads panel was drawn from; a row click queues the rest.
let panelTracks = [];

// Object URLs pin their Blob until revoked, so the previous panel's are dropped.
let panelCoverUrls = [];

export function deviceTrackIds() {
  return panelTracks.map((track) => track.id);
}

function releasePanelCovers() {
  for (const url of panelCoverUrls) URL.revokeObjectURL(url);
  panelCoverUrls = [];
}

// Also run on every fragment swap: the swapped-in Library tile carries the
// server's placeholder text.
async function syncDeviceSummary() {
  const tileCount = document.getElementById("downloads-card-count");
  if (!tileCount || !deviceStorageSupported()) return;
  const count = (await savedTrackIds()).length;
  tileCount.textContent = count ? `${count} song${count === 1 ? "" : "s"}` : "Nothing saved yet";
}

let saveAbort = null;

/**
 * True while a playing element is still buffering (< HAVE_FUTURE_DATA): pulling
 * another whole file then is what makes the track stall.
 */
function playbackNeedsTheConnection() {
  const audio = activeAudio();
  if (!audio || audio.paused) return false;
  return audio.readyState < HTMLMediaElement.HAVE_FUTURE_DATA;
}

// Favorites and hand-made playlists are named to the server, so their current tracks are asked
// for on every sync. Anything else (an album, a YouTube playlist) is kept as the ids it had.
function keyFor(source) {
  if (!source) return null;
  if (source.kind === "favorites") return "favorites";
  if (source.kind === "user-playlist") return `playlist:${source.id}`;
  return `list:${source.kind}:${source.id}`;
}

function listRequest(key, retry) {
  const query = retry ? "?retry=true" : "";
  if (key === "favorites") return api(`/offline/favorites${query}`, { method: "POST" });
  if (key.startsWith("playlist:")) {
    return api(`/offline/playlists/${key.slice("playlist:".length)}${query}`, { method: "POST" });
  }
  return api(`/offline/tracks${query}`, { method: "POST", body: { key, ids: kept.get(key)?.ids || [] } });
}

// Unkept lists whose server pins couldn't be dropped yet (no connection). Until they are,
// the server keeps their songs as downloads instead of cache.
const UNPIN_PENDING_KEY = "spotea-unpin-pending";

function pendingUnpins() {
  try {
    return JSON.parse(localStorage.getItem(UNPIN_PENDING_KEY) || "[]");
  } catch {
    return [];
  }
}

function rememberPendingUnpins(keys) {
  try {
    if (keys.length) localStorage.setItem(UNPIN_PENDING_KEY, JSON.stringify(keys));
    else localStorage.removeItem(UNPIN_PENDING_KEY);
  } catch {
    /* Lost with the session: the songs just stay downloads on the server. */
  }
}

async function unpin(key) {
  const { ok } = await api(`/offline/lists?key=${encodeURIComponent(key)}`, { method: "DELETE" });
  return ok;
}

async function flushPendingUnpins() {
  const left = [];
  for (const key of pendingUnpins()) {
    // Kept again since: its next sync pins it afresh.
    if (kept.has(key)) continue;
    if (!(await unpin(key))) left.push(key);
  }
  rememberPendingUnpins(left);
}

// Kept lists (key -> { title, ids, videoIds }), mirrored from IndexedDB so painting and the
// save loop needn't read it.
const kept = new Map();

// The open panel's source, set by detail.js; the download button's key is derived from it.
let panelSource = null;

// key -> { done, total, failed } from the last pass, for the list's download button.
const progress = new Map();

/**
 * One list: the server queues what it lacks, the device is matched to the list, then
 * server-ready tracks are saved one at a time. Gives way to playback: the missing
 * track is often the one currently buffering.
 */
async function syncList(key, { retry = false } = {}) {
  const res = await listRequest(key, retry);
  if (!res.ok) {
    // Deleted on the server: nothing left to keep. No response at all is just offline.
    if (res.status === 404) {
      await reconcileList(key, null);
      kept.delete(key);
      progress.delete(key);
    }
    return { waiting: false, deferred: false };
  }

  const tracks = res.data?.tracks || [];
  const present = new Set(await reconcileList(key, tracks.map((track) => track.id)));
  const failed = tracks.filter((track) => track.is_unavailable || track.status === "error").length;
  const report = () => {
    progress.set(key, { done: present.size, total: tracks.length, failed });
    paintKeepButton();
  };
  report();

  let deferred = false;
  let failure = null;
  for (const track of tracks) {
    if (present.has(track.id) || track.status !== "ready") continue;
    // Checked per track: a run is minutes long; the list may be unkept or playback may start mid-run.
    if (!kept.has(key)) break;
    if (playbackNeedsTheConnection()) {
      deferred = true;
      break;
    }
    const controller = new AbortController();
    saveAbort = controller;
    try {
      await saveTrack(
        track.id,
        {
          title: track.title,
          artist: track.channel_title || "",
          coverUrl: track.thumbnail_url || null,
          duration: track.duration_seconds ?? null,
        },
        { signal: controller.signal, list: key }
      );
      present.add(track.id);
      report();
    } catch (err) {
      // Aborted by the player's `waiting` handler: deferred, not a failure.
      if (err?.name === "AbortError") {
        deferred = true;
        break;
      }
      // A full device fails every remaining save the same way, so stop here.
      failure = err?.message || "Could not save one of these songs";
      break;
    } finally {
      saveAbort = null;
    }
  }
  if (failure) showToast(failure);

  const waiting = tracks.some(
    (track) => !present.has(track.id) && (track.queued || track.status === "downloading")
  );
  return { waiting, deferred };
}

// One pass at a time: concurrent passes would fetch a track twice and orphan the loser's bytes.
let syncing = false;
let syncAgain = false;

// Lists just switched on: their earlier failures are worth one more try.
const retryKeys = new Set();

async function syncKeptLists() {
  if (!deviceStorageSupported() || document.body.classList.contains("is-offline")) return;
  if (syncing) {
    syncAgain = true;
    return;
  }
  syncing = true;
  let waiting = false;
  let deferred = false;
  try {
    await flushPendingUnpins();
    for (const key of [...kept.keys()]) {
      const outcome = await syncList(key, { retry: retryKeys.delete(key) });
      waiting ||= outcome.waiting;
      deferred ||= outcome.deferred;
    }
  } finally {
    syncing = false;
    await syncDeviceSummary();
    markDeviceRows();
  }

  if (syncAgain) {
    syncAgain = false;
    syncKeptLists();
  } else if (deferred) {
    scheduleSync(SYNC_RETRY_DELAY);
  } else if (waiting) {
    scheduleSync(SYNC_POLL_DELAY);
  }
}

async function toggleKeep(button) {
  const key = button.dataset.keepList;
  if (!key) return;
  if (!deviceStorageSupported()) {
    showToast("This browser can't keep songs on the device");
    return;
  }

  if (!kept.has(key)) {
    const title = button.dataset.keepTitle || "";
    const list = { title, ids: null, videoIds: null };
    if (key.startsWith("list:")) {
      if (button.disabled) return;
      button.disabled = true;
      const made = await materializeRemoteRows(undefined, "Could not download this list");
      button.disabled = false;
      if (!made) return;
      list.ids = made.data.content_ids;
      list.videoIds = made.items.map((item) => item.video_id);
    }
    try {
      await keepList(key, title, list);
    } catch {
      showToast("Could not keep this list on the device");
      return;
    }
    kept.set(key, list);
    // Asked only once the user commits to keeping something; an unprompted request is likelier refused.
    requestPersistence();
    retryKeys.add(key);
    paintKeepButton();
    syncKeptLists();
    return;
  }

  // Read off the device, not `progress`: before the first sync after a load, that is still empty.
  const done = (await listSaved()).filter((track) => track.lists?.includes(key)).length;
  if (done) {
    const confirmed = await confirmDialog(
      `Remove this list's ${done} song${done === 1 ? "" : "s"} from this device? ` +
        "Songs another downloaded list holds stay, and everything still plays with a connection.",
      "Remove"
    );
    if (!confirmed) return;
  }
  kept.delete(key);
  progress.delete(key);
  try {
    await reconcileList(key, null);
  } catch {
    showToast("Could not clear this list from the device");
  }
  if (!(await unpin(key))) rememberPendingUnpins([...new Set([...pendingUnpins(), key])]);
  decorateDetailPanel();
  await syncDeviceSummary();
}

/** The open list's download button: off, "12/40" while saving, or on. */
function paintKeepButton() {
  const button = document.getElementById("detail-keep-btn");
  const key = button?.dataset.keepList;
  if (!key) return;
  const on = kept.has(key);
  const state = progress.get(key);

  button.classList.toggle("is-on", on);
  button.setAttribute("aria-pressed", String(on));
  button.title = on ? "Remove download" : "Download";
  button.setAttribute("aria-label", on ? "Remove download" : "Download to this device");

  // Unavailable and failed tracks never arrive, so they don't hold the count open.
  const reachable = state ? state.total - state.failed : 0;
  const saving = on && state && state.done < reachable;
  const label = button.querySelector(".keep-progress");
  if (label) {
    label.hidden = !saving;
    label.textContent = saving ? `${state.done}/${reachable}` : "";
  }
}

async function markDeviceRows() {
  const rows = document.querySelectorAll("#detail-panel .track-row");
  if (!rows.length || !deviceStorageSupported()) return;
  const onDevice = new Set((await savedTrackIds()).map(Number));

  // Remote rows carry no content id; a kept list remembers which id each video became.
  const list = kept.get(keyFor(panelSource));
  const idForVideo = new Map((list?.videoIds || []).map((videoId, i) => [videoId, list.ids[i]]));

  for (const row of rows) {
    const id = row.dataset.contentId ?? idForVideo.get(row.dataset.videoId);
    row.classList.toggle("is-on-device", id != null && onDevice.has(Number(id)));
  }
}

/** detail.js calls this after every panel swap, with the panel's { kind, id }. */
export function decorateDetailPanel(source = panelSource) {
  panelSource = source;
  const button = document.getElementById("detail-keep-btn");
  const key = keyFor(source);
  if (button && key) button.dataset.keepList = key;
  paintKeepButton();
  markDeviceRows();
}

async function forgetOne(button) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    await deleteTrack(button.dataset.contentId);
  } catch {
    showToast("Could not remove that song from this device");
    button.disabled = false;
    return;
  }
  await syncDeviceSummary();
  await renderDownloadsPanel();
}

function isDownloadsPanelOpen() {
  return Boolean(document.getElementById("downloads-panel"));
}

function rowHtml(track, index) {
  const duration = track.duration ? formatDuration(track.duration) : "";
  // Only copies no list holds (saved before lists existed): a kept list's next sync would put the rest back.
  const forget = track.lists?.length
    ? ""
    : `<button type="button" class="track-forget" data-content-id="${track.id}" aria-label="Remove from this device">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-close" /></svg>
      </button>`;
  return `
    <div class="track-row" data-content-id="${track.id}" data-status="ready">
      <a class="track-link" href="/player/${track.id}" aria-label="Play ${escapeHtml(track.title)}">
        <span class="track-index">${index}</span>
        <span class="track-thumb" data-cover-for="${track.id}"></span>
        <span class="track-info">
          <span class="track-title" title="${escapeHtml(track.title)}">${escapeHtml(track.title)}</span>
          <span class="track-artist">${escapeHtml(track.artist || "")}</span>
        </span>
      </a>
      <span class="track-duration">${duration}</span>
      ${forget}
    </div>`;
}

/**
 * Covers are a second pass (each is an IndexedDB read). The stored `coverUrl`
 * is never a fallback: /image-proxy is a request, on the screen that must make none.
 */
export async function renderDownloadsPanel() {
  const panel = document.getElementById("detail-panel");
  if (!panel) return;

  releasePanelCovers();
  panelTracks = await listSaved();

  const bytes = panelTracks.reduce((total, track) => total + (track.size || 0), 0);
  const countLine = panelTracks.length
    ? `${panelTracks.length} song${panelTracks.length === 1 ? "" : "s"} · ${formatSize(bytes)}`
    : "Nothing on this device yet";

  const actions = panelTracks.length
    ? `<div class="detail-actions">
         <button type="button" id="detail-play-all" class="btn-play-all" aria-label="Play all" title="Play all">
           <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><use href="#i-play" /></svg>
         </button>
         <button type="button" id="detail-shuffle" class="btn-quiet-icon btn-shuffle" aria-pressed="false" aria-label="Shuffle" title="Shuffle">
           <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-shuffle" /></svg>
         </button>
       </div>`
    : "";

  const body = panelTracks.length
    ? `<div class="track-list">${panelTracks.map((track, i) => rowHtml(track, i + 1)).join("")}</div>`
    : `<div class="empty-state">
         <p class="empty-state-title">Nothing saved to this device</p>
         <p class="empty-state-help">Tap the download button on a playlist or on Favorites, and its songs land here to play with no connection at all.</p>
       </div>`;

  panel.innerHTML = `
    <div id="downloads-panel">
      <button type="button" class="back-link" id="detail-back-btn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-arrow-left" /></svg>
        Library
      </button>
      <div class="channel-hero">
        <span class="channel-hero-avatar channel-card-icon is-downloads">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-phone" /></svg>
        </span>
        <div class="channel-hero-info">
          <h1 class="channel-hero-title">Downloads</h1>
          <p class="muted">${countLine}</p>
        </div>
        ${actions}
      </div>
      ${body}
    </div>`;

  for (const track of panelTracks) {
    const url = await openCoverUrl(track.id);
    if (!url) continue;
    const slot = panel.querySelector(`.track-thumb[data-cover-for="${track.id}"]`);
    if (!slot) {
      // Panel replaced mid-read: revoke, or the URL pins its Blob forever.
      URL.revokeObjectURL(url);
      continue;
    }
    panelCoverUrls.push(url);
    // Created, not pre-rendered hidden: `.track-thumb img { display: block }`
    // beats [hidden], so an empty <img> would show as broken.
    const img = document.createElement("img");
    img.alt = "";
    img.src = url;
    slot.replaceChildren(img);
  }
}

// With no connection, only Library and the Downloads panel remain reachable.
function lockToOfflineSurface() {
  const tab = document.documentElement.dataset.activeTab;
  if (tab === "library") return;
  if (tab === "detail" && isDownloadsPanelOpen()) return;
  activate("library");
}

export function setupOfflineMode() {
  document.addEventListener(CONNECTION_CHANGED, (event) => {
    if (!event.detail?.offline) return;
    lockToOfflineSurface();
  });

  // Already offline at boot (SW-cached open): the event above will never fire.
  if (document.body.classList.contains("is-offline")) lockToOfflineSurface();
}

export async function setupDeviceStorage() {
  // Stalled mid-track: the in-flight save is the likeliest cause and can wait.
  onPlayerEvent("waiting", () => saveAbort?.abort());

  // Delegated: #detail-panel's children are replaced on every open.
  document.getElementById("detail-panel")?.addEventListener("click", (event) => {
    const forget = event.target.closest(".track-forget");
    if (forget) {
      forgetOne(forget);
      return;
    }
    const keep = event.target.closest("#detail-keep-btn");
    if (keep) toggleKeep(keep);
  });

  onFragmentsSwapped(() => {
    syncDeviceSummary();
    // Debounced: refreshFragments fires on every play, favourite and playlist edit, and each pass
    // asks the server about every kept list. It's also how a song added to a kept list comes down.
    if (kept.size) scheduleSync();
  });

  syncDeviceSummary();
  for (const list of await keptLists()) {
    kept.set(list.key, { title: list.title, ids: list.ids ?? null, videoIds: list.videoIds ?? null });
  }
  decorateDetailPanel();
  if (kept.size) scheduleSync(SYNC_BOOT_DELAY);
}

// Collapses a burst of refreshes into one pass.
const SYNC_DELAY = 8000;

// Out of the way of the page's own first requests.
const SYNC_BOOT_DELAY = 3000;

// The server is still downloading some of a list; its queue lands a song every second or two.
const SYNC_POLL_DELAY = 3000;

// Longer: the run stopped because the player is struggling for the connection.
const SYNC_RETRY_DELAY = 30000;

let syncTimer = null;

function scheduleSync(delay = SYNC_DELAY) {
  if (syncTimer !== null) return;
  syncTimer = setTimeout(() => {
    syncTimer = null;
    syncKeptLists();
  }, delay);
}
