// What this device holds (IndexedDB via ../offline.js) and the offline lock.

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
  clearAll as clearDeviceCopies,
  deleteTrack,
  deviceUsage,
  isSupported as deviceStorageSupported,
  listSaved,
  offlinePlaybackOn,
  openCoverUrl,
  rememberOfflinePlayback,
  requestPersistence,
  saveTrack,
} from "../offline.js";
import { activeAudio, onPlayerEvent } from "../player.js";
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

function syncSwitch() {
  const toggle = document.getElementById("offline-playback-toggle");
  if (toggle) toggle.checked = offlinePlaybackOn();
}

// Also run on every fragment swap: the swapped-in Library tile carries the
// server's placeholder text.
export async function syncDeviceSummary() {
  const line = document.getElementById("device-summary-text");
  const tileCount = document.getElementById("downloads-card-count");
  const toggle = document.getElementById("offline-playback-toggle");

  // No IndexedDB (private mode): a switch that silently fails is worse than none.
  if (!deviceStorageSupported()) {
    if (toggle) toggle.disabled = true;
    if (line) line.textContent = "This browser can't keep songs on the device.";
    return;
  }

  const { count, bytes, quota } = await deviceUsage();

  if (tileCount) {
    tileCount.textContent = count ? `${count} song${count === 1 ? "" : "s"}` : "Nothing saved yet";
  }
  if (!line) return;
  line.dataset.count = String(count);
  if (!count) {
    line.textContent = "Keep every download on this phone, so it plays with no connection.";
    return;
  }
  // "about": the quota is advisory, origin-wide, and unpredictable on iOS.
  const ceiling = quota ? ` of about ${formatSize(quota)}` : "";
  line.textContent = `${count} song${count === 1 ? "" : "s"} on this device · ${formatSize(bytes)}${ceiling}`;
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

// One sync at a time: concurrent passes would fetch a track twice and orphan
// the loser's bytes.
let syncing = false;

function reportProgress(text) {
  const line = document.getElementById("device-summary-text");
  if (line) line.textContent = text;
}

/**
 * Saves everything downloaded that isn't on the device yet, sequentially. Gives
 * way to playback: the missing track is often the one currently buffering.
 */
async function syncDevice({ announce = false } = {}) {
  if (syncing || !deviceStorageSupported()) return;
  syncing = true;
  try {
    const { ok, data } = await api("/storage/items", {
      errorMessage: announce ? "Could not read your downloads" : undefined,
    });
    if (!ok) return;

    const items = data || [];
    const { ids } = await deviceUsage();
    const already = new Set(ids.map(Number));
    const pending = items.filter((item) => !already.has(Number(item.id)));
    if (!pending.length) {
      if (announce) showToast("Everything is already on this device");
      return;
    }

    // Requested only once the user commits to keeping something; an unprompted
    // request before there's anything to protect is likelier to be refused.
    await requestPersistence();

    let saved = 0;
    let failure = null;
    let deferred = false;
    for (const item of pending) {
      // Checked per track: a run is minutes long and playback may start mid-run.
      if (playbackNeedsTheConnection()) {
        deferred = true;
        break;
      }
      reportProgress(`Saving ${saved + 1} of ${pending.length}…`);
      const controller = new AbortController();
      saveAbort = controller;
      try {
        await saveTrack(
          item.id,
          {
            title: item.title,
            artist: item.channel_title || "",
            coverUrl: item.thumbnail_url || null,
            duration: item.duration_seconds ?? null,
          },
          { signal: controller.signal }
        );
        saved += 1;
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

    if (failure) showToast(`Saved ${saved} of ${pending.length}. ${failure}`);
    else if (deferred) {
      if (announce) showToast(`Saving ${pending.length} songs in the background`);
      scheduleTopUp(TOP_UP_RETRY_DELAY);
    } else if (announce) {
      showToast(`Saved ${saved} song${saved === 1 ? "" : "s"} to this device`);
    }
  } finally {
    syncing = false;
    await syncDeviceSummary();
  }
}

async function enableOfflinePlayback(toggle) {
  if (!deviceStorageSupported()) {
    showToast("This browser can't keep songs on the device");
    toggle.checked = false;
    return;
  }
  rememberOfflinePlayback(true);
  await syncDevice({ announce: true });
}

async function disableOfflinePlayback(toggle) {
  const { count, bytes } = await deviceUsage();
  if (count) {
    const confirmed = await confirmDialog(
      `Turn off offline playback and remove the ${count} song${count === 1 ? "" : "s"} ` +
        `kept on this device (${formatSize(bytes)})? ` +
        "The downloads on the server stay, so these play again whenever you have a connection.",
      "Turn off"
    );
    if (!confirmed) {
      toggle.checked = true;
      return;
    }
    try {
      await clearDeviceCopies();
    } catch {
      showToast("Could not clear this device's copies");
    }
  }
  rememberOfflinePlayback(false);
  await syncDeviceSummary();
  if (isDownloadsPanelOpen()) await renderDownloadsPanel();
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
  // No × with offline playback on: the next top-up would just put it back.
  const forget = offlinePlaybackOn()
    ? ""
    : `<button type="button" class="track-forget" data-content-id="${track.id}" aria-label="Remove from this device">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-close" /></svg>
      </button>`;
  return `
    <div class="track-row" data-content-id="${track.id}" data-status="ready">
      <a class="track-link" href="/#player/${track.id}" aria-label="Play ${escapeHtml(track.title)}">
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
         <p class="empty-state-help">Settings → Songs on this device → Save all puts your downloads here, and they play with no connection at all.</p>
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

export function setupDeviceStorage() {
  const toggle = document.getElementById("offline-playback-toggle");
  toggle?.addEventListener("change", () => {
    if (toggle.checked) enableOfflinePlayback(toggle);
    else disableOfflinePlayback(toggle);
  });

  // Stalled mid-track: the in-flight save is the likeliest cause and can wait.
  onPlayerEvent("waiting", () => saveAbort?.abort());

  // Delegated: #detail-panel's children are replaced on every open.
  document.getElementById("detail-panel")?.addEventListener("click", (event) => {
    const forget = event.target.closest(".track-forget");
    if (forget) forgetOne(forget);
  });

  onFragmentsSwapped(() => {
    syncDeviceSummary();
    // Debounced via scheduleTopUp: refreshFragments fires on every play and
    // favourite, and each pass asks the server for the full download list.
    if (offlinePlaybackOn()) scheduleTopUp();
  });

  syncSwitch();
  syncDeviceSummary();
  if (offlinePlaybackOn()) scheduleTopUp();
}

// Collapses a burst of refreshes into one pass.
const TOP_UP_DELAY = 8000;

// Longer: the run stopped because the player is struggling for the connection.
const TOP_UP_RETRY_DELAY = 30000;

let topUpTimer = null;

function scheduleTopUp(delay = TOP_UP_DELAY) {
  if (topUpTimer !== null) return;
  topUpTimer = setTimeout(() => {
    topUpTimer = null;
    syncDevice();
  }, delay);
}
