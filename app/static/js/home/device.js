// What this device itself is holding, and what the app is while there is no
// connection.
//
// Three things that look separate and are not:
//
//   * Settings' "Offline playback" switch — one control that keeps the phone
//     stocked with everything the server has, instead of the per-row toggle
//     the Downloads modal used to carry on every single line.
//   * Library's "Downloads" tile and the panel it opens, which is the only
//     track list in the app whose rows do not come from the server. They
//     come out of IndexedDB (see ../offline.js), because the moment this
//     panel matters is the moment nothing can be fetched.
//   * The offline lock: with no connection, everything that needs one is
//     shut off and Library's Downloads tile is what is left.
//
// They share one fact — which content ids are on this device — and keeping
// that fact in one module is why they are here together rather than spread
// across settings.js, detail.js and a fourth file.

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
  openCoverUrl,
  requestPersistence,
  saveTrack,
} from "../offline.js";
import { activate } from "./tabs.js";

// The rows the open Downloads panel was built from, in the order it drew
// them. Read back when one is clicked so the queue is the rest of this list
// — the same thing clicking a row means anywhere else in the app, except
// that here there is no /content/queue endpoint to ask for it.
let panelTracks = [];

// Object URLs handed to the panel's <img> tags. An object URL pins its Blob
// until it is revoked, and these are whole cover images, so the previous
// panel's are dropped before the next one's are made.
let panelCoverUrls = [];

/** Every content id currently on this device, in the panel's order. */
export function deviceTrackIds() {
  return panelTracks.map((track) => track.id);
}

function releasePanelCovers() {
  for (const url of panelCoverUrls) URL.revokeObjectURL(url);
  panelCoverUrls = [];
}

/* -------------------------------------------------------------------------
   The summary line, in Settings and on Library's tile
   ---------------------------------------------------------------------- */

/**
 * Re-reads IndexedDB and pushes the answer at both places that show it.
 *
 * Registered against every fragment swap as well as called directly: the
 * Library grid (and so its Downloads tile) is replaced wholesale by
 * refreshFragments, and a swapped-in tile comes back with the server's
 * placeholder text on it.
 */
/** Puts the switch back in step with the stored preference. */
function syncSwitch() {
  const toggle = document.getElementById("offline-playback-toggle");
  if (toggle) toggle.checked = offlinePlaybackOn();
}

export async function syncDeviceSummary() {
  const line = document.getElementById("device-summary-text");
  const tileCount = document.getElementById("downloads-card-count");
  const toggle = document.getElementById("offline-playback-toggle");

  // A browser with no IndexedDB (private mode, chiefly) cannot keep anything,
  // and a switch that silently fails is worse than no switch.
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
  // "about", because the browser's quota figure is advisory, covers the whole
  // origin rather than this feature, and on iOS is both smaller and less
  // predictable than elsewhere. Shown anyway: a device that refuses the next
  // save is a lot less mysterious when the ceiling was visible beforehand.
  const ceiling = quota ? ` of about ${formatSize(quota)}` : "";
  line.textContent = `${count} song${count === 1 ? "" : "s"} on this device · ${formatSize(bytes)}${ceiling}`;
}

/* -------------------------------------------------------------------------
   Offline playback: one switch that keeps this device stocked
   ---------------------------------------------------------------------- */

// A device preference, not an account one — the same login on a laptop and a
// phone wants different answers, and the server has no business holding
// either. localStorage rather than a cookie for the same reason: nothing
// about this ever needs to reach a request.
const OFFLINE_PREF_KEY = "spotea-offline-playback";

export function offlinePlaybackOn() {
  try {
    return localStorage.getItem(OFFLINE_PREF_KEY) === "1";
  } catch {
    // Private browsing with storage disabled. The copies could not be kept
    // either, so "off" is the only honest answer.
    return false;
  }
}

function rememberOfflinePlayback(on) {
  try {
    if (on) localStorage.setItem(OFFLINE_PREF_KEY, "1");
    else localStorage.removeItem(OFFLINE_PREF_KEY);
  } catch {
    /* Nothing to remember it with; the switch still works for this session. */
  }
}

// One sync at a time. The switch, the boot pass and the top-up after a new
// download can all ask at once, and two passes would fetch the same track
// twice — both would write, and the loser's bytes would be orphaned under a
// record the winner had already replaced.
let syncing = false;

/** Says what the sync is doing, in the line that otherwise holds the total. */
function reportProgress(text) {
  const line = document.getElementById("device-summary-text");
  if (line) line.textContent = text;
}

/**
 * Brings this device up to date with the server: everything downloaded that
 * is not here yet, one at a time.
 *
 * Sequential deliberately. Each save is a whole audio file, and firing forty
 * at a household server over a phone's connection is how a convenience turns
 * into a stall — the line reports progress instead, which is what makes the
 * wait legible.
 *
 * `announce` is off for the background top-up: that one runs after a track
 * finishes downloading, where a toast for something nobody asked for is just
 * noise.
 */
async function syncDevice({ announce = false } = {}) {
  if (syncing || !deviceStorageSupported()) return;
  syncing = true;
  try {
    const { ok, data } = await api("/storage/items", {
      errorMessage: announce ? "Could not read your downloads" : undefined,
    });
    // Offline, or the server said no. Nothing to do and nothing to say — the
    // switch stays on and the next top-up picks this up.
    if (!ok) return;

    const items = data || [];
    const { ids } = await deviceUsage();
    const already = new Set(ids.map(Number));
    const pending = items.filter((item) => !already.has(Number(item.id)));
    if (!pending.length) {
      if (announce) showToast("Everything is already on this device");
      return;
    }

    // Asked for at the moment the user first commits to keeping something,
    // not on boot: an unprompted permission request before there is anything
    // to protect is the kind a browser is most likely to refuse.
    await requestPersistence();

    let saved = 0;
    let failure = null;
    for (const item of pending) {
      reportProgress(`Saving ${saved + 1} of ${pending.length}…`);
      try {
        await saveTrack(item.id, {
          title: item.title,
          artist: item.channel_title || "",
          coverUrl: item.thumbnail_url || null,
          duration: item.duration_seconds ?? null,
        });
        saved += 1;
      } catch (err) {
        // A full device ends the run rather than skipping one song: every
        // remaining save would fail the same way, and forty toasts saying so
        // is not a better answer than one.
        failure = err?.message || "Could not save one of these songs";
        break;
      }
    }

    if (failure) showToast(`Saved ${saved} of ${pending.length}. ${failure}`);
    else if (announce) showToast(`Saved ${saved} song${saved === 1 ? "" : "s"} to this device`);
  } finally {
    syncing = false;
    await syncDeviceSummary();
  }
}

/** The switch was turned on: keep everything, starting with what is already
 *  downloaded. */
async function enableOfflinePlayback(toggle) {
  if (!deviceStorageSupported()) {
    showToast("This browser can't keep songs on the device");
    toggle.checked = false;
    return;
  }
  rememberOfflinePlayback(true);
  await syncDevice({ announce: true });
}

/** And off again, which means the copies go — that is the only thing being
 *  "on" ever did. Confirmed, because it is the one action here that destroys
 *  something the app cannot get back without a connection. */
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
      // Nothing changed, so the switch has to go back to saying so.
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
  // The open panel, if this was pressed with one behind Settings, is now a
  // list of songs that are not there.
  if (isDownloadsPanelOpen()) await renderDownloadsPanel();
}

/** Forgets one track's bytes, from a row in the Downloads panel. */
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

/* -------------------------------------------------------------------------
   The Downloads panel
   ---------------------------------------------------------------------- */

function isDownloadsPanelOpen() {
  return Boolean(document.getElementById("downloads-panel"));
}

function rowHtml(track, index) {
  const duration = track.duration ? formatDuration(track.duration) : "";
  // Only when nothing is going to put it straight back. With offline playback
  // on, this device is meant to hold everything the server has — a × that the
  // next top-up undoes reads as broken rather than obeyed, so the way to drop
  // one song is to turn the switch off.
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
 * Draws the panel from IndexedDB, and fills each row's cover from the blob
 * saved beside its audio.
 *
 * The covers are a second pass rather than part of the markup above because
 * each one is an IndexedDB read: doing them inline would hold the whole list
 * behind the slowest of them, for artwork nobody is waiting on. The stored
 * `coverUrl` is deliberately not used as a fallback — it points at
 * /image-proxy, which is a request, on the one screen that must make none.
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
      // The panel was replaced while this read was in flight — the URL has
      // no slot to belong to and would otherwise pin its Blob forever.
      URL.revokeObjectURL(url);
      continue;
    }
    panelCoverUrls.push(url);
    // Created rather than rendered hidden above and revealed here:
    // .track-thumb img is display: block, which beats the [hidden]
    // attribute, so a placeholder <img> with no src would render as a broken
    // image for every track whose cover was never saved.
    const img = document.createElement("img");
    img.alt = "";
    img.src = url;
    slot.replaceChildren(img);
  }
}

/* -------------------------------------------------------------------------
   The offline lock
   ---------------------------------------------------------------------- */

// Where the app is allowed to be with no connection: Library, and the one
// detail view whose contents do not come from the server.
function lockToOfflineSurface() {
  const tab = document.documentElement.dataset.activeTab;
  if (tab === "library") return;
  if (tab === "detail" && isDownloadsPanelOpen()) return;
  activate("library");
}

export function setupOfflineMode() {
  document.addEventListener(CONNECTION_CHANGED, (event) => {
    if (!event.detail?.offline) return;
    // Everything else in the app is a request away, and CSS has already
    // dimmed the controls that lead there (see style.css's body.is-offline).
    // This is for wherever the user already was when the connection went.
    lockToOfflineSurface();
  });

  // The connection can already be gone by the time this runs — an offline
  // open is the whole case the service worker's cached shell exists for, and
  // it is the one where nothing will ever fire the event above.
  if (document.body.classList.contains("is-offline")) lockToOfflineSurface();
}

export function setupDeviceStorage() {
  const toggle = document.getElementById("offline-playback-toggle");
  toggle?.addEventListener("change", () => {
    if (toggle.checked) enableOfflinePlayback(toggle);
    else disableOfflinePlayback(toggle);
  });

  // Delegated from #detail-panel, whose children are replaced on every panel
  // open — a listener on a row would go with them.
  document.getElementById("detail-panel")?.addEventListener("click", (event) => {
    const forget = event.target.closest(".track-forget");
    if (forget) forgetOne(forget);
  });

  // Library's grid is inside the fragment refreshFragments replaces, so its
  // tile comes back carrying the server's placeholder count each time.
  onFragmentsSwapped(() => {
    syncDeviceSummary();
    // A fragment refresh is what happens after a track finishes downloading,
    // which is exactly when this device is one song behind the server. Not on
    // the swap itself: refreshFragments fires on every play and every
    // favourite too, and asking the server for the full download list that
    // often would be a request per tap.
    if (offlinePlaybackOn()) scheduleTopUp();
  });

  syncSwitch();
  syncDeviceSummary();
  // Whatever arrived while this device was closed, or was left half-done by
  // a save that ran out of room and has since been given some.
  if (offlinePlaybackOn()) scheduleTopUp();
}

// Long enough that a burst of refreshes (a play, then its download finishing,
// then a favourite) collapses into one pass, and short enough that a song is
// on the device before the phone is put down.
const TOP_UP_DELAY = 8000;
let topUpTimer = null;

function scheduleTopUp() {
  if (topUpTimer !== null) return;
  topUpTimer = setTimeout(() => {
    topUpTimer = null;
    syncDevice();
  }, TOP_UP_DELAY);
}
