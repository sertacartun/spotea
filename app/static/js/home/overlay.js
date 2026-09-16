// In-page player overlay + mini bar; drives player.js's setup against the shared controls DOM across many tracks.

import { applyAmbientTint } from "./ambient.js";
import { api, formatDuration, showToast } from "../core.js";
import { refreshFragments, refreshQueuePanel } from "../fragments.js";
import {
  openCoverUrl,
  openTrackUrl,
  readTrackMeta,
} from "../offline.js";
import {
  activeAudio,
  applyNowPlayingMetadata,
  clearNowPlayingMetadata,
  clearPreparing,
  loadedTrackId,
  nextPollDelay,
  offerPrefetchedAudio,
  onPlayerEvent,
  paintRange,
  prepareAudio,
  releaseAudio,
  reportMediaSessionAction,
  reportPlayback,
  setNowPlayingArtwork,
  showPreparing,
  whenVisible,
} from "../player.js";
import { clearResumeState, readResumeState } from "../resume.js";
import {
  QUEUE_CHANGED,
  clearQueue,
  currentId,
  cycleRepeat,
  isShuffled,
  loadQueue,
  nextId,
  noteCurrent,
  peekNextId,
  peekPreviousId,
  previousId,
  queueOrder,
  repeatMode,
  toggleShuffle,
} from "./queue.js";

// Matches player.js's SKIP_SECONDS.
const SEEK_STEP_SECONDS = 15;

// How long a prefetch follows its download; past this the handoff prepares the track the ordinary way.
const UPCOMING_POLL_BUDGET_MS = 30000;

// Wide enough that a backgrounded iOS page's ~1Hz timeupdate still ticks inside the window.
const EARLY_HANDOFF_SECONDS = 1.2;

// Tracks run ~1.3 MB; this only declines anomalies (hour-long uploads), which then stream normally.
const PREFETCH_MAX_BYTES = 24 * 1024 * 1024;

// Next track prefetched (metadata + in-memory audio) so `ended` can start it synchronously;
// any await there lets a suspending iOS page defer the handoff indefinitely.
let upcomingTrack = null;

// Abortable: on a miss the leftover transfer competes with <audio> fetching the very same file.
let upcomingAbort = null;

function abortUpcomingFetch() {
  upcomingAbort?.abort();
  upcomingAbort = null;
}

let prefetchedFor = null;

// Idempotent; fired on open, queue change and timeupdate. The open matters: a stalled element
// emits no timeupdate, so relying on it alone chains one stall into the next.
function prefetchUpcoming() {
  const playing = document.getElementById("player-root")?.dataset.contentId;
  if (!playing || prefetchedFor === playing) return;
  const upcoming = peekNextId();
  if (upcoming == null) return;
  prefetchedFor = playing;
  cacheUpcoming(upcoming);
}

let playerCoverUrl = null;

// A systemic YouTube block makes every track look broken, and skipping through them all
// rapidly is itself what trips the bot check.
const MAX_CONSECUTIVE_AUTO_SKIPS = 3;
let consecutiveAutoSkipFailures = 0;

// Unavailable skips never hit YouTube, so a looser cap that only guarantees termination.
const MAX_CONSECUTIVE_UNAVAILABLE_SKIPS = 10;
let consecutiveUnavailableSkips = 0;

// Dispatched instead of calling detail.js's openDetail, which would be an import cycle. detail: `{ pageId }`.
export const OPEN_ARTIST = "spotea:open-artist";

function expandPlayer() {
  document.getElementById("player-overlay").hidden = false;
}

function collapsePlayer() {
  setQueueOpen(false);
  document.getElementById("player-overlay").hidden = true;
}

// coverSrc passed in: offline it's a blob: URL, and thumbnail_url would hit /image-proxy.
function syncMiniPlayerInfo(data, coverSrc) {
  document.getElementById("mini-player-title").textContent = data.title;
  document.getElementById("mini-player-channel").textContent = data.channel_title || "";
  const img = document.getElementById("mini-player-art-img");
  if (coverSrc) {
    img.src = coverSrc;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

export async function openPlayer(contentId, { expanded = true, requireVisible = true } = {}) {
  contentId = String(contentId);
  const root = document.getElementById("player-root");

  // Every route lands here; opens from outside a queue must drop it (see queue.js's noteCurrent).
  noteCurrent(contentId);

  if (root.dataset.contentId === contentId) {
    expandPlayer();
    return;
  }

  // Never pause() the outgoing track: on iOS that ends the background-audio grant and later
  // off-screen play() calls are ignored. Reassigning audio.src swaps it while keeping the session.

  const wasOpen = Boolean(root.dataset.contentId);

  // Taken, not read: the cached copy is only valid for this one handoff.
  let data = null;
  let prefetchedAudio = null;
  if (upcomingTrack && upcomingTrack.id === contentId) {
    data = upcomingTrack.data;
    prefetchedAudio = upcomingTrack.objectUrl;
    reportPlayback("handoff-cached", { contentId, status: data.status, buffered: Boolean(prefetchedAudio) });
  } else {
    // `prepared` separates "the prefetch never ran" from "it ran for a different track".
    reportPlayback("handoff-missed", { contentId, prepared: upcomingTrack?.id ?? null });
    if (upcomingTrack?.objectUrl) {
      // An object URL pins its Blob until revoked.
      URL.revokeObjectURL(upcomingTrack.objectUrl);
    }
  }
  upcomingTrack = null;
  abortUpcomingFetch();

  // Device lookup only on a miss: a prefetch hit must stay await-free for the frozen-iOS auto-advance.
  let playingFromDevice = false;
  if (!prefetchedAudio) {
    const savedUrl = await openTrackUrl(contentId);
    if (savedUrl) {
      prefetchedAudio = savedUrl;
      playingFromDevice = true;
      reportPlayback("handoff-device", { contentId });
    }
  }

  // player.js takes ownership and revokes it; null is passed too, to release a stale offer.
  offerPrefetchedAudio(contentId, prefetchedAudio);

  if (!data) {
    // Only with a card on screen; a cold open has no card to put into the preparing state.
    if (wasOpen) showPreparing();

    const res = await api(`/content/${contentId}`);
    // Offline, fall back to metadata stored with a saved track — only on no response (status 0);
    // a 404/409 is a real answer and wins over the local copy.
    if (!res.ok && res.status === 0 && playingFromDevice) {
      const meta = await readTrackMeta(contentId);
      if (meta) {
        data = {
          id: Number(contentId),
          title: meta.title,
          channel_title: meta.artist || "",
          // Unknowable offline; `offline` makes the controls disable rather than mislead.
          artist_page_id: null,
          is_favorite: false,
          offline: true,
          // Cover comes off the device below; /image-proxy would be a request on a no-network path.
          thumbnail_url: null,
          duration_seconds: meta.duration ?? null,
          status: "ready",
          is_unavailable: false,
        };
        reportPlayback("opened-offline", { contentId });
      }
    }
    if (!data && !res.ok) {
      showToast(
        res.status === 0
          ? "You're offline — this song isn't saved to this device"
          : "Could not load this track"
      );
      // Otherwise the transport stays disabled and the still-playing track can't be paused.
      if (wasOpen) clearPreparing();
      // The resume record only clears on successful playback, so a bad one would fail every page load.
      clearResumeState();
      // A local miss, never a YouTube request, so it takes the unavailable cap and still auto-advances.
      const upcoming = peekNextId();
      if (upcoming != null) {
        consecutiveUnavailableSkips += 1;
        if (consecutiveUnavailableSkips < MAX_CONSECUTIVE_UNAVAILABLE_SKIPS) {
          playFromQueue(nextId());
        } else {
          showToast("Too many unavailable tracks in a row — stopping here");
        }
      }
      return;
    }
    // songVersionOf is a live lookup; skip it when the offline fallback built `data`.
    if (!data) {
      data = res.data;
      data = await songVersionOf(data);
    }
  }

  document.querySelector(".player-title").textContent = data.title;
  const channelBtn = document.querySelector(".player-channel");
  channelBtn.textContent = data.channel_title || "";
  channelBtn.dataset.artistPageId = data.artist_page_id || "";
  channelBtn.disabled = !data.artist_page_id;
  const artImg = document.getElementById("player-art-img");
  // Saved cover for device playback: thumbnail_url is a network request.
  const savedCover = playingFromDevice ? await openCoverUrl(contentId) : null;
  if (playerCoverUrl) URL.revokeObjectURL(playerCoverUrl);
  playerCoverUrl = savedCover;
  const coverSrc = savedCover || data.thumbnail_url;
  if (coverSrc) {
    artImg.src = coverSrc;
    artImg.hidden = false;
  } else {
    artImg.removeAttribute("src");
    artImg.hidden = true;
  }
  // Separate from the <img>: Now Playing wants sized variants and can't fetch blob: URLs.
  setNowPlayingArtwork(data.artwork);
  applyAmbientTint();
  document.getElementById("duration-time").textContent = data.duration_seconds
    ? formatDuration(data.duration_seconds)
    : "0:00";

  const favBtn = document.getElementById("favorite-btn");
  favBtn.disabled = data.offline === true;
  favBtn.dataset.contentId = data.id;
  favBtn.dataset.favorite = String(data.is_favorite);
  favBtn.classList.toggle("is-on", data.is_favorite);
  favBtn.setAttribute("aria-pressed", String(data.is_favorite));
  favBtn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

  root.dataset.contentId = String(data.id);
  // A saved copy overrides server state: "not_downloaded" would re-download and
  // is_unavailable would skip a track whose bytes are right here.
  root.dataset.status = playingFromDevice ? "ready" : data.status;
  root.dataset.unavailable = String(!playingFromDevice && data.is_unavailable === true);
  root.dataset.stream = `/content/${data.id}/stream`;

  syncMiniPlayerInfo(data, coverSrc);

  // Republished per open (setupMediaSession reads the DOM once). iOS may drop this pre-playback
  // publish, so player.js re-publishes on `playing`; both are needed.
  applyNowPlayingMetadata();

  document.getElementById("player-overlay").hidden = !expanded;
  document.getElementById("mini-player").hidden = false;
  document.body.classList.add("has-mini-player");

  // Before prepareAudio, so the next track's prep runs alongside this download rather than behind it.
  prefetchUpcoming();

  const start = () => {
    prepareAudio(
      () => {
        // Reset here (right after audio.src changes), not up front, or the bar blanks under a still-playing track.
        const seekBar = document.getElementById("seek-bar");
        seekBar.value = 0;
        document.getElementById("current-time").textContent = "0:00";
        paintRange(seekBar);
        document.getElementById("mini-player-progress-fill").style.width = "0%";

        // Stated explicitly: a prefetched track makes no /stream request that could imply the play.
        api(`/content/${data.id}/played`, { method: "POST" }).then(() => refreshFragments());
        consecutiveAutoSkipFailures = 0;
        consecutiveUnavailableSkips = 0;
      },
      (message, { permanent } = {}) => {
        // Skip broken tracks, notably on background auto-advance where nobody can press next.
        const upcoming = peekNextId();
        if (upcoming == null) return;

        if (permanent) {
          consecutiveUnavailableSkips += 1;
          if (consecutiveUnavailableSkips >= MAX_CONSECUTIVE_UNAVAILABLE_SKIPS) {
            showToast("Too many unavailable tracks in a row — stopping here");
            return;
          }
          showToast(`"${data.title}" isn't available on YouTube — skipping`);
          playFromQueue(nextId());
          return;
        }

        consecutiveAutoSkipFailures += 1;
        if (consecutiveAutoSkipFailures >= MAX_CONSECUTIVE_AUTO_SKIPS) {
          showToast("Several tracks in a row failed — stopping instead of skipping further");
          return;
        }
        showToast(`Couldn't play "${data.title}" — skipping to the next track`);
        playFromQueue(nextId());
      }
    );
  };

  // Queue handoffs skip whenVisible: a locked screen reads as hidden, so waiting would stall background
  // advance. Safe because a queue only exists after an earlier track passed this gate.
  if (requireVisible) whenVisible(start);
  else start();
}

/** Swaps a music-video row for its song version (square art, lyrics); best effort. */
async function songVersionOf(data) {
  if (!data.is_music_video) return data;
  const { ok, data: resolved } = await api(`/content/${data.id}/song-version`, { method: "POST" });
  return ok && resolved ? resolved : data;
}

/** Prefetches the next track (download, metadata, audio) into `upcomingTrack`; best effort. */
async function cacheUpcoming(contentId) {
  const id = String(contentId);
  abortUpcomingFetch();

  // The server swaps in the song version before downloading, so the returned row is already swapped.
  const download = await api(`/content/${id}/download`, { method: "POST" });

  let data = null;
  if (download.data?.content) {
    data = { ...download.data.content };
    data.status = download.data.status;
    data.is_unavailable = download.data.is_unavailable === true;
  } else {
    // Usually a 409 (already downloading elsewhere): fetch the row separately.
    const meta = await api(`/content/${id}`);
    if (!meta.ok) return;
    data = { ...meta.data };
  }

  // Never publish the unswapped row: the handoff renders straight from this.
  upcomingTrack = { id, data, objectUrl: null };

  if (data.is_unavailable || data.status === "error") return;
  if (data.status === "ready") {
    await cacheUpcomingAudio(id);
    return;
  }

  const startedAt = Date.now();
  while (Date.now() - startedAt < UPCOMING_POLL_BUDGET_MS) {
    await new Promise((resolve) => setTimeout(resolve, nextPollDelay(Date.now() - startedAt)));
    if (upcomingTrack?.id !== id) return;
    const { ok, data: status } = await api(`/content/${id}/status`);
    if (!ok) continue;
    upcomingTrack.data.status = status.status;
    upcomingTrack.data.is_unavailable = status.is_unavailable === true;
    if (status.status === "error") return;
    if (status.status === "ready") {
      await cacheUpcomingAudio(id);
      return;
    }
  }
}

/** Buffers the next track's audio in memory so the handoff is a src swap, not a network fetch. */
async function cacheUpcomingAudio(id) {
  let objectUrl = null;
  // Hoisted so finally only clears upcomingAbort if it's still ours, not a newer prefetch's.
  let controller = null;
  try {
    // Resolved at prefetch time so the frozen-iOS handoff finds the bytes already in the page.
    const savedUrl = await openTrackUrl(id);
    if (savedUrl) {
      if (upcomingTrack?.id !== id) {
        URL.revokeObjectURL(savedUrl);
        return;
      }
      upcomingTrack.objectUrl = savedUrl;
      return;
    }

    // /stream doesn't record a play, which is what makes fetching early safe.
    controller = new AbortController();
    upcomingAbort = controller;
    const res = await fetch(`/content/${id}/stream`, { signal: controller.signal });
    if (!res.ok) return;
    const declared = Number(res.headers.get("content-length"));
    if (Number.isFinite(declared) && declared > PREFETCH_MAX_BYTES) return;
    const blob = await res.blob();
    if (blob.size > PREFETCH_MAX_BYTES) return;
    objectUrl = URL.createObjectURL(blob);
  } catch (err) {
    return;
  } finally {
    if (upcomingAbort === controller) upcomingAbort = null;
  }

  if (upcomingTrack?.id !== id) {
    URL.revokeObjectURL(objectUrl);
    return;
  }
  upcomingTrack.objectUrl = objectUrl;
}

/** Keeps the overlay as the user left it: auto-advance and lock-screen controls must not pop it open. */
function playFromQueue(contentId) {
  if (contentId == null) return;
  openPlayer(contentId, {
    expanded: !document.getElementById("player-overlay").hidden,
    requireVisible: false,
  });
}

function markCurrentQueueRow() {
  const playing = currentId();
  for (const row of document.querySelectorAll("#queue-panel-body .track-row")) {
    row.classList.toggle("is-current", Number(row.dataset.contentId) === playing);
  }
}

// Must match the `min-width: 900px` breakpoint by .player-main in style.css.
const pinnedPanel = window.matchMedia("(min-width: 900px)");

function setQueueOpen(open) {
  const panel = document.getElementById("queue-panel");
  const toggle = document.getElementById("queue-toggle");
  const overlay = document.getElementById("player-overlay");
  if (!panel || !toggle || !overlay) return;
  // Pinned open on wide screens; forced here so every close request is a no-op there.
  if (pinnedPanel.matches) open = true;
  panel.classList.toggle("is-open", open);
  overlay.classList.toggle("is-queue-open", open);
  toggle.classList.toggle("is-on", open);
  toggle.setAttribute("aria-expanded", String(open));
}

// Past the wobble in a tap, well short of a deliberate pull.
const QUEUE_DRAG_CLOSE_PX = 48;

function setupQueuePanel() {
  const toggle = document.getElementById("queue-toggle");
  const panel = document.getElementById("queue-panel");
  if (!toggle || !panel) return;

  // Lets QUEUE_CHANGED tell "the pointer moved" from "the list is different".
  let rendered = [];

  const load = async () => {
    const order = queueOrder();
    const ok = await refreshQueuePanel(order);
    if (ok) rendered = order;
    markCurrentQueueRow();
    return ok;
  };

  toggle.addEventListener("click", () => {
    const opening = !panel.classList.contains("is-open");
    // Open first; rows land whenever. Waiting on them made the button feel dead.
    setQueueOpen(opening);
    if (opening) load();
  });

  // Pulling the tab strip (the sheet's top edge) down closes the panel.
  const handle = panel.querySelector(".panel-tabs");
  if (!handle) return;
  let dragFrom = null;
  // A time window, not a swallow-next-click flag: touch drags don't always emit a click,
  // and the flag then ate the next real tap.
  let closedByDragAt = 0;
  const CLICK_AFTER_DRAG_MS = 400;

  handle.addEventListener("pointerdown", (event) => {
    if (pinnedPanel.matches) return;
    if (!panel.classList.contains("is-open")) return;
    dragFrom = event.clientY;
  });
  handle.addEventListener("pointermove", (event) => {
    if (dragFrom === null || event.clientY - dragFrom < QUEUE_DRAG_CLOSE_PX) return;
    dragFrom = null;
    closedByDragAt = Date.now();
    setQueueOpen(false);
  });
  const endDrag = () => {
    dragFrom = null;
  };
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);
  // Capture: stops the post-drag click before lyrics.js's tab handlers switch tabs.
  handle.addEventListener(
    "click",
    (event) => {
      if (Date.now() - closedByDragAt > CLICK_AFTER_DRAG_MS) return;
      closedByDragAt = 0;
      event.stopPropagation();
      event.preventDefault();
    },
    true
  );

  // Pointer-only moves just re-mark the row; re-fetching would rebuild the list under the user.
  document.addEventListener(QUEUE_CHANGED, () => {
    if (!panel.classList.contains("is-open")) return;
    const order = queueOrder();
    if (order.length === rendered.length && order.every((id, i) => id === rendered[i])) {
      markCurrentQueueRow();
      return;
    }
    load();
  });

  const syncPinned = () => {
    setQueueOpen(pinnedPanel.matches);
    if (pinnedPanel.matches) load();
  };
  pinnedPanel.addEventListener("change", syncPinned);
  if (pinnedPanel.matches) syncPinned();
}

function syncQueueControls() {
  const hasNext = peekNextId() !== null;
  const hasPrevious = peekPreviousId() !== null;

  document.getElementById("next-track").disabled = !hasNext;
  document.getElementById("prev-track").disabled = !hasPrevious;
  // Hidden on the bar but disabled in the overlay, whose row must keep its shape.
  document.getElementById("mini-player-next").hidden = !hasNext;
  document.getElementById("mini-player-prev").hidden = !hasPrevious;

  const shuffleBtn = document.getElementById("player-shuffle");
  shuffleBtn.classList.toggle("is-on", isShuffled());
  shuffleBtn.setAttribute("aria-pressed", String(isShuffled()));

  // The label names the state: the two icons differ only by a numeral.
  const repeat = repeatMode();
  const repeatBtn = document.getElementById("player-repeat");
  repeatBtn.dataset.repeat = repeat;
  repeatBtn.classList.toggle("is-on", repeat !== "off");
  repeatBtn.setAttribute(
    "aria-label",
    { off: "Repeat off", all: "Repeat queue", one: "Repeat this song" }[repeat]
  );
  // toggleAttribute: SVGElement has no `hidden` property, so `.hidden =` silently does nothing.
  document.getElementById("icon-repeat").toggleAttribute("hidden", repeat === "one");
  document.getElementById("icon-repeat-one").toggleAttribute("hidden", repeat !== "one");

  if (!("mediaSession" in navigator)) return;
  // Null, not a no-op: a handler's presence makes the OS draw the button.
  // Wrapped because unsupported actions throw.
  try {
    navigator.mediaSession.setActionHandler(
      "nexttrack",
      hasNext
        ? () => {
            reportMediaSessionAction("nexttrack");
            playFromQueue(nextId());
          }
        : null
    );
    navigator.mediaSession.setActionHandler(
      "previoustrack",
      hasPrevious
        ? () => {
            reportMediaSessionAction("previoustrack");
            playFromQueue(previousId());
          }
        : null
    );
  } catch (err) {
    /* Not supported here — the in-page transport still works. */
  }
}

export function closePlayer() {
  const audio = activeAudio();
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  releaseAudio();

  const root = document.getElementById("player-root");
  root.dataset.contentId = "";
  root.dataset.status = "";
  root.dataset.unavailable = "";
  root.dataset.stream = "";
  if (upcomingTrack?.objectUrl) URL.revokeObjectURL(upcomingTrack.objectUrl);
  upcomingTrack = null;

  setQueueOpen(false);
  document.getElementById("player-overlay").hidden = true;
  document.getElementById("mini-player").hidden = true;
  document.body.classList.remove("has-mini-player");

  clearQueue();

  clearNowPlayingMetadata();
}

export function setupPlayerOverlay() {
  const overlay = document.getElementById("player-overlay");
  if (!overlay) return;

  const miniPlayBtn = document.getElementById("mini-player-playpause");
  const miniIconPlay = document.getElementById("mini-icon-play");
  const miniIconPause = document.getElementById("mini-icon-pause");

  const syncMiniIcon = () => {
    const paused = activeAudio().paused;
    miniIconPlay.toggleAttribute("hidden", !paused);
    miniIconPause.toggleAttribute("hidden", paused);
    miniPlayBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
  };

  const miniProgress = document.getElementById("mini-player-progress");
  const miniProgressFill = document.getElementById("mini-player-progress-fill");
  const miniTime = document.getElementById("mini-player-time");

  const syncMiniProgress = () => {
    const audio = activeAudio();
    const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    miniProgressFill.style.width = `${pct}%`;
    miniProgress.setAttribute("aria-valuenow", String(Math.round(pct)));
    miniTime.textContent = audio.duration
      ? `${formatDuration(audio.currentTime)} / ${formatDuration(audio.duration)}`
      : "";
  };

  const seekToEventX = (event) => {
    const audio = activeAudio();
    if (!audio.duration) return;
    const box = miniProgress.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    audio.currentTime = ratio * audio.duration;
    syncMiniProgress();
  };

  miniProgress.addEventListener("click", seekToEventX);
  miniProgress.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowLeft" ? -SEEK_STEP_SECONDS : event.key === "ArrowRight" ? SEEK_STEP_SECONDS : 0;
    if (!step) return;
    // Otherwise the document-level arrow handler in player.js scrubs a second
    // time and the track jumps twice as far.
    event.preventDefault();
    event.stopPropagation();
    const audio = activeAudio();
    audio.currentTime = Math.min(audio.duration || 0, Math.max(0, audio.currentTime + step));
    syncMiniProgress();
  });

  miniPlayBtn.addEventListener("click", () => {
    const audio = activeAudio();
    if (audio.paused) audio.play().catch(() => {});
    else audio.pause();
  });
  onPlayerEvent("play", syncMiniIcon);
  onPlayerEvent("pause", syncMiniIcon);
  onPlayerEvent("ended", syncMiniIcon);
  onPlayerEvent("timeupdate", syncMiniProgress);
  onPlayerEvent("loadedmetadata", syncMiniProgress);

  onPlayerEvent("ended", () => {
    const root = document.getElementById("player-root");
    const finished = root.dataset.contentId;
    // Switches don't stop the outgoing track, so it can end while the next downloads; don't advance past it.
    if (finished && loadedTrackId() !== finished) {
      reportPlayback("outgoing-ended", { contentId: finished });
      return;
    }
    // Rewind rather than reopen, so looping keeps the loaded resource.
    if (repeatMode() === "one") {
      const audio = activeAudio();
      audio.currentTime = 0;
      reportPlayback("track-ended", { contentId: finished, next: finished, repeat: "one" });
      audio.play().catch(() => {});
      return;
    }
    const next = nextId();
    // `buffered`: whether the handoff swaps to an in-memory blob or goes to the network.
    reportPlayback("track-ended", {
      contentId: finished,
      next,
      prepared: upcomingTrack?.id ?? null,
      buffered: Boolean(upcomingTrack?.objectUrl),
    });
    // Queue ran out: clear Now Playing, or iOS leaves a dead card on the Dynamic Island.
    if (next == null) clearNowPlayingMetadata();
    playFromQueue(next);
  });

  // The open is the main prefetch trigger; these catch opens that had no queue yet
  // ("Play all" builds it after opening the first track).
  document.addEventListener(QUEUE_CHANGED, prefetchUpcoming);
  onPlayerEvent("timeupdate", prefetchUpcoming);

  // Background early handoff: start the next track before `ended`, since iOS may freeze a hidden page
  // right after it. Only when hidden and with bytes in memory; repeat-one rewinds early for the same reason.
  let earlyHandoffFor = null;
  onPlayerEvent("timeupdate", () => {
    if (document.visibilityState === "visible") return;
    const audio = activeAudio();
    if (audio.paused) return;
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    if (audio.duration - audio.currentTime > EARLY_HANDOFF_SECONDS) return;

    const playing = document.getElementById("player-root").dataset.contentId;
    // A switch already underway (DOM describes a track the element hasn't been handed yet).
    if (!playing || loadedTrackId() !== playing) return;

    if (repeatMode() === "one") {
      audio.currentTime = 0;
      return;
    }

    if (earlyHandoffFor === playing) return;
    const next = peekNextId();
    if (next == null) return;
    if (upcomingTrack?.id !== String(next) || !upcomingTrack.objectUrl) return;
    earlyHandoffFor = playing;
    reportPlayback("early-handoff", { contentId: playing, next });
    playFromQueue(nextId());
  });

  document.getElementById("prev-track").addEventListener("click", () => playFromQueue(previousId()));
  document.getElementById("next-track").addEventListener("click", () => playFromQueue(nextId()));
  document.getElementById("mini-player-next").addEventListener("click", () => playFromQueue(nextId()));
  document.getElementById("mini-player-prev").addEventListener("click", () => playFromQueue(previousId()));
  document.getElementById("player-shuffle").addEventListener("click", () => toggleShuffle());
  document.getElementById("player-repeat").addEventListener("click", () => cycleRepeat());

  setupQueuePanel();

  document.addEventListener(QUEUE_CHANGED, syncQueueControls);
  syncQueueControls();

  // iOS drops setActionHandler before the audio session exists, so a queue's first track would get
  // ±15s seek buttons instead of next/previous; re-sync once audio is playing.
  onPlayerEvent("playing", syncQueueControls);

  // Collapse, not close: closing stops the music, and leaving it up hides the artist page.
  document.querySelector(".player-channel").addEventListener("click", (event) => {
    const pageId = event.currentTarget.dataset.artistPageId;
    if (!pageId) return;
    collapsePlayer();
    document.dispatchEvent(new CustomEvent(OPEN_ARTIST, { detail: { pageId } }));
  });

  document.getElementById("mini-player-expand").addEventListener("click", expandPlayer);
  document.getElementById("overlay-collapse-btn").addEventListener("click", (event) => {
    event.preventDefault();
    collapsePlayer();
  });
  document.getElementById("mini-player-close").addEventListener("click", closePlayer);

  const homeTab = document.getElementById("tab-home");
  if (!homeTab) return;

  homeTab.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    const link = event.target.closest("a");
    if (!link) return;
    const card = event.target.closest(".card");
    if (!card) return;

    event.preventDefault();
    const contentId = card.dataset.contentId;
    openPlayer(contentId);

    // Queue the rest of the shelf, not awaited: a round trip before play() loses the iOS user gesture.
    const kind = card.closest("[data-queue-kind]")?.dataset.queueKind;
    if (kind) loadQueue({ kind }, { startId: contentId });
  });
}

// The overlay starts every page load empty, so resume has to reopen it explicitly.
export function resumeOverlayIfNeeded() {
  const root = document.getElementById("player-root");
  if (!root || root.dataset.contentId) return;
  const saved = readResumeState();
  // !== false so older records without this flag default to expanded.
  if (saved?.contentId) openPlayer(saved.contentId, { expanded: saved.wasExpanded !== false });
}
