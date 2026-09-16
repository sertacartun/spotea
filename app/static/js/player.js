// Its only caller is the in-page overlay (home/overlay.js).

import { api, formatDuration, showToast } from "./core.js";
import { refreshFragments } from "./fragments.js";
import { consumeResumeState } from "./resume.js";

const SKIP_SECONDS = 15;

// Poll tightly while downloads typically land (~2.5-3.7s end to end), then back off.
const POLL_TIGHT_MS = 200;
const POLL_TIGHT_UNTIL_MS = 4000;
const POLL_RELAXED_MS = 500;
const POLL_RELAXED_UNTIL_MS = 12000;
const POLL_STEADY_MS = 2000;

// Elapsed-time based so slow responses can't shift the schedule; also drives overlay.js's prefetch poll.
export function nextPollDelay(elapsedMs) {
  if (elapsedMs < POLL_TIGHT_UNTIL_MS) return POLL_TIGHT_MS;
  if (elapsedMs < POLL_RELAXED_UNTIL_MS) return POLL_RELAXED_MS;
  return POLL_STEADY_MS;
}

// Survives across prepareAudio calls so a later track cancels an earlier poll before it hijacks playback.
let activePollTimer = null;

// setTimeout chain, so the owning call is re-checked between ticks.
let activePollToken = null;

// Removed on the next call, or a stale handler could startPlayback() an old track on visibilitychange.
let activeVisibilityHandler = null;

// Breadcrumbs for playback that silently doesn't happen (see routers/debug.py). sendBeacon survives a
// page being frozen; failures are swallowed so diagnostics can never break playback.

// Only these reach the server; call sites fire unconditionally. Keep it to events that signal
// something went wrong or explain why playback stopped — happy-path events are pure noise.
const REPORTED_EVENTS = new Set([
  "play-rejected",
  "playback-stalled",
  "prepare-failed",
  "outgoing-ended",
  "track-ended",
  "retry-rejected",
  "visibility-changed",
  "media-session-action",
  "audio-session",
  "early-handoff",
  "handoff-cached",
  "handoff-device",
  "handoff-missed",
]);

export function reportPlayback(event, detail = {}) {
  if (!REPORTED_EVENTS.has(event)) return;
  try {
    // audioSession is Safari-only; stamped on every beacon to catch interruptions at failure time.
    const body = JSON.stringify([
      {
        event,
        visibility: document.visibilityState,
        audioSession: navigator.audioSession?.state ?? "unsupported",
        at: new Date().toISOString(),
        ...detail,
      },
    ]);
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/debug/playback", new Blob([body], { type: "application/json" }));
    } else {
      fetch("/debug/playback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        keepalive: true,
      }).catch(() => {});
    }
  } catch (err) {
    /* Never let a breadcrumb take playback down with it. */
  }
}

/** One beacon per lock-screen/headset tap, capturing element state before the handler acts. */
export function reportMediaSessionAction(action) {
  const audio = activeAudio();
  reportPlayback("media-session-action", {
    action,
    contentId: document.getElementById("player-root")?.dataset.contentId,
    paused: audio ? audio.paused : null,
    readyState: audio ? audio.readyState : null,
  });
}

// The only <audio> in the document: a second one (decks, keep-alive clip) breaks iOS Now Playing.
// Never pause() it on a track switch — that releases the iOS audio session in the background.
export function activeAudio() {
  return document.getElementById("audio");
}

// Tracked, not derived from audio.src: a prefetched blob: URL has no id, and a wrong answer makes
// startPlayback reset a playing track to 0:00 and overlay.js's `ended` handler stop advancing.
let loadedContentId = null;

// Held so it can be revoked; an object URL pins its whole audio Blob.
let loadedObjectUrl = null;

// Offered ahead of the src assignment that adopts it; this module revokes it either way.
let pendingObjectUrl = null;
let pendingObjectUrlFor = null;

export function loadedTrackId() {
  return loadedContentId;
}

/** Shows the preparing state before metadata arrives, so a Next press doesn't read as ignored. */
export function showPreparing(message = "Preparing audio…") {
  const prepare = document.getElementById("prepare-state");
  if (!prepare) return;
  prepare.hidden = false;
  prepare.classList.remove("is-error");
  prepare.querySelector(".spinner").hidden = false;
  document.getElementById("prepare-text").textContent = message;
  document.querySelector(".transport")?.classList.add("is-disabled");
}

export function clearPreparing() {
  const prepare = document.getElementById("prepare-state");
  if (!prepare) return;
  prepare.hidden = true;
  document.querySelector(".transport")?.classList.remove("is-disabled");
}

/** Offers in-page bytes for `contentId`; a null url releases an untaken earlier offer. */
export function offerPrefetchedAudio(contentId, objectUrl = null) {
  if (pendingObjectUrl && pendingObjectUrl !== objectUrl) URL.revokeObjectURL(pendingObjectUrl);
  pendingObjectUrl = objectUrl;
  pendingObjectUrlFor = objectUrl ? String(contentId) : null;
}

export function releaseAudio() {
  offerPrefetchedAudio(null, null);
  if (loadedObjectUrl) URL.revokeObjectURL(loadedObjectUrl);
  loadedObjectUrl = null;
  loadedContentId = null;
}

export function onPlayerEvent(type, handler) {
  activeAudio()?.addEventListener(type, handler);
}

const SILENT_AUDIO_DATA_URI =
  "data:audio/wav;base64,UklGRiUAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQEAAACA";

// WebKit only allows play() synchronously inside a gesture, but the unlock sticks to the element, so
// play it once on the first real click. Not from openPlayer, which also runs without a gesture.
let audioUnlocked = false;
function unlockAudio() {
  if (audioUnlocked) return;

  const audio = activeAudio();
  // Don't stomp a loaded source, and don't set the flag: a boot-time resume assigns src without a
  // gesture, so the element is still locked and a later click must get to unlock it.
  if (!audio || audio.src) return;

  audioUnlocked = true;
  // WebKit needs the call inside the click, not the resolved promise, so don't await before pausing.
  audio.src = SILENT_AUDIO_DATA_URI;
  audio.play().catch(() => {});
  audio.pause();
}

export function paintRange(input) {
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--fill", `${pct}%`);
}

// Sniffed rather than feature-detected: iOS accepts and reads back audio.volume yet ignores it for output.
function isIOSWebKit() {
  const ua = navigator.userAgent || "";
  if (/iPad|iPhone|iPod/.test(ua)) return true;
  // iPadOS 13+ identifies itself as a Mac; the touch points give it away.
  return /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
}

function volumeIsSettable(audio) {
  const original = audio.volume;
  try {
    audio.volume = original === 0.5 ? 0.4 : 0.5;
    const settable = audio.volume !== original;
    audio.volume = original;
    return settable;
  } catch {
    return false;
  }
}

export function setupPlayer() {
  if (!activeAudio()) return;

  document.addEventListener("click", unlockAudio);

  const playBtn = document.getElementById("play-pause");
  const iconPlay = document.getElementById("icon-play");
  const iconPause = document.getElementById("icon-pause");
  const seek = document.getElementById("seek-bar");
  const currentTimeEl = document.getElementById("current-time");
  const durationEl = document.getElementById("duration-time");
  const volume = document.getElementById("volume-bar");
  const muteBtn = document.getElementById("mute-btn");
  const iconVolume = document.getElementById("icon-volume");
  const iconMuted = document.getElementById("icon-muted");

  let scrubbing = false;

  // SVGElement has no `hidden` property; `.hidden =` never sets the attribute, so use toggleAttribute.
  function showIcon(el, visible) {
    el.toggleAttribute("hidden", !visible);
  }

  function syncPlayIcon() {
    const paused = activeAudio().paused;
    showIcon(iconPlay, paused);
    showIcon(iconPause, !paused);
    playBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
  }

  function syncMuteIcon() {
    const audio = activeAudio();
    const silent = audio.muted || audio.volume === 0;
    showIcon(iconVolume, !silent);
    showIcon(iconMuted, silent);
    muteBtn.setAttribute("aria-label", silent ? "Unmute" : "Mute");
  }

  playBtn.addEventListener("click", () => {
    const audio = activeAudio();
    if (audio.paused) audio.play().catch(() => showToast("Playback was blocked by the browser"));
    else audio.pause();
  });

  onPlayerEvent("play", syncPlayIcon);
  onPlayerEvent("pause", syncPlayIcon);
  onPlayerEvent("ended", syncPlayIcon);

  onPlayerEvent("loadedmetadata", () => {
    const audio = activeAudio();
    seek.max = audio.duration || 0;
    durationEl.textContent = formatDuration(audio.duration);
    paintRange(seek);
  });

  onPlayerEvent("timeupdate", () => {
    if (scrubbing) return;
    const audio = activeAudio();
    seek.value = audio.currentTime;
    currentTimeEl.textContent = formatDuration(audio.currentTime);
    paintRange(seek);
  });

  onPlayerEvent("error", () => {
    // Ignore failures of unlockAudio's silent clip; the toast would claim a song won't play.
    if (activeAudio().src === SILENT_AUDIO_DATA_URI) return;
    showToast("Could not load the audio for this track");
  });

  seek.addEventListener("input", () => {
    scrubbing = true;
    currentTimeEl.textContent = formatDuration(Number(seek.value));
    paintRange(seek);
  });
  seek.addEventListener("change", () => {
    activeAudio().currentTime = Number(seek.value);
    scrubbing = false;
  });

  // iOS ignores audio.volume (and a GainNode) — hardware buttons own volume, so the slider is hidden.
  // Don't route through Web Audio instead: a suspended background AudioContext is silence.
  const volumeIsNative = volumeIsSettable(activeAudio());
  if (volumeIsNative && !isIOSWebKit()) {
    volume.addEventListener("input", () => {
      const audio = activeAudio();
      audio.volume = Number(volume.value) / 100;
      audio.muted = false;
      paintRange(volume);
      syncMuteIcon();
    });
  } else {
    volume.hidden = true;
  }

  muteBtn.addEventListener("click", () => {
    activeAudio().muted = !activeAudio().muted;
    syncMuteIcon();
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, button, a")) return;
    const audio = activeAudio();
    if (event.code === "Space") {
      event.preventDefault();
      playBtn.click();
    } else if (event.code === "ArrowLeft") {
      audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
    } else if (event.code === "ArrowRight") {
      audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
    }
  });

  syncPlayIcon();
  syncMuteIcon();
  paintRange(seek);
  paintRange(volume);
  setupMediaSession();
}

// Dedup key for the last publish. Only skip identical re-publishes once one landed while iOS held
// the session (`playing`); a pre-playback publish may have been silently dropped.
let publishedNowPlaying = null;
let publishedWhileHeld = false;

// Typed only when the extension states it; iOS may draw grey squares for untyped artwork.
const ARTWORK_TYPES = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp" };

// Sized variants from the server: iOS won't downscale a large entry (grey Dynamic Island art)
// and can't fetch the blob: URL #player-art-img may hold.
let nowPlayingArtwork = [];

export function setNowPlayingArtwork(artwork) {
  nowPlayingArtwork = Array.isArray(artwork) ? artwork : [];
}

// Fallback for payloads without artwork.
function artworkFromElement() {
  const src = document.getElementById("player-art-img")?.src || "";
  if (!src) return [];
  const extension = src.split("?")[0].split(".").pop()?.toLowerCase();
  const type = ARTWORK_TYPES[extension];
  return [type ? { src, type } : { src }];
}

export function applyNowPlayingMetadata({ held = false } = {}) {
  if (!("mediaSession" in navigator)) return;
  const title = document.querySelector(".player-title")?.textContent || "";
  const artist = document.querySelector(".player-channel")?.textContent || "";
  const artwork = nowPlayingArtwork.length ? nowPlayingArtwork : artworkFromElement();

  // Entries in one set are the same picture, so the first URL identifies it.
  const key = [title, artist, artwork[0]?.src || ""].join("\u0000");
  if (key === publishedNowPlaying && publishedWhileHeld) return;
  publishedNowPlaying = key;
  publishedWhileHeld = held;

  navigator.mediaSession.metadata = new MediaMetadata({ title, artist, artwork });
}

/** Clears Now Playing and the dedup state; a stale card otherwise lingers on the Dynamic Island. */
export function clearNowPlayingMetadata() {
  if (!("mediaSession" in navigator)) return;
  publishedNowPlaying = null;
  publishedWhileHeld = false;
  navigator.mediaSession.metadata = null;
  navigator.mediaSession.playbackState = "none";
  nowPlayingArtwork = [];
}

function setupMediaSession() {
  if (!("mediaSession" in navigator)) return;

  applyNowPlayingMetadata();

  if ("audioSession" in navigator) {
    // Declares a media player so iOS keeps it running when locked and under the silent switch.
    // Wrapped: implementations may refuse the assignment.
    try {
      navigator.audioSession.type = "playback";
    } catch (err) {
      /* Stays "auto" — exactly what every non-Safari browser does anyway. */
    }

    // Otherwise OS interruptions look identical to the user pausing.
    if (typeof navigator.audioSession.addEventListener === "function") {
      navigator.audioSession.addEventListener("statechange", () => {
        reportPlayback("audio-session", { state: navigator.audioSession.state });
      });
    }
  }

  navigator.mediaSession.setActionHandler("play", () => {
    reportMediaSessionAction("play");
    activeAudio()
      .play()
      .catch((err) =>
        reportPlayback("play-rejected", {
          contentId: document.getElementById("player-root")?.dataset.contentId,
          error: String(err?.name || err),
          via: "media-session",
        })
      );
  });
  navigator.mediaSession.setActionHandler("pause", () => {
    reportMediaSessionAction("pause");
    activeAudio().pause();
  });
  navigator.mediaSession.setActionHandler("seekbackward", () => {
    const audio = activeAudio();
    audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
  });
  navigator.mediaSession.setActionHandler("seekforward", () => {
    const audio = activeAudio();
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
  });
  navigator.mediaSession.setActionHandler("seekto", (details) => {
    if (details.seekTime != null) activeAudio().currentTime = details.seekTime;
  });

  // Only `playing` means sound: backgrounded iOS may accept play() (paused=false) and render nothing.
  // Not cleared on `waiting`, so buffering hitches don't flap the lock screen.
  let rendering = false;
  const setRendering = (on) => {
    rendering = on;
    navigator.mediaSession.playbackState = on ? "playing" : "paused";
  };

  const syncPositionState = () => {
    // No position while silent: the OS extrapolates elapsed time locally, and playbackRate 0 is a TypeError.
    if (!rendering) return;
    const audio = activeAudio();
    // Number.isFinite: setPositionState throws on an Infinity duration too.
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    try {
      navigator.mediaSession.setPositionState({
        duration: audio.duration,
        // Clamped: mid-seek, position > duration is a TypeError.
        position: Math.min(audio.currentTime, audio.duration),
        playbackRate: audio.playbackRate || 1,
      });
    } catch (err) {
      /* Never let the lock screen take playback down with it. */
    }
  };
  onPlayerEvent("loadedmetadata", syncPositionState);
  onPlayerEvent("timeupdate", syncPositionState);

  onPlayerEvent("pause", () => setRendering(false));
  onPlayerEvent("ended", () => setRendering(false));

  // `playing` is the only moment iOS reliably accepts Now Playing updates; re-assert everything here.
  onPlayerEvent("playing", () => {
    setRendering(true);
    // held: marks this publish trustworthy so later `playing` events can skip identical re-publishes.
    applyNowPlayingMetadata({ held: true });
    syncPositionState();
    reportPlayback("now-playing", {
      contentId: document.getElementById("player-root")?.dataset.contentId,
      playbackState: navigator.mediaSession.playbackState,
      paused: activeAudio().paused,
    });
  });
}

// Prerender and background tabs run JS unseen; opening a track starts a download, so wait until visible.
export function whenVisible(run) {
  if (document.prerendering) {
    document.addEventListener("prerenderingchange", () => whenVisible(run), { once: true });
    return;
  }
  if (document.visibilityState !== "visible") {
    const onChange = () => {
      if (document.visibilityState === "visible") {
        document.removeEventListener("visibilitychange", onChange);
        run();
      }
    };
    document.addEventListener("visibilitychange", onChange);
    return;
  }
  run();
}

/**
 * onStart fires once when the element loads the track;
 * onFail gets `{ permanent }` (unavailable on YouTube vs. possibly systemic) for the skip caps.
 */
export async function prepareAudio(onStart, onFail) {
  const root = document.getElementById("player-root");
  const prepare = document.getElementById("prepare-state");
  const prepareText = document.getElementById("prepare-text");
  const transport = document.querySelector(".transport");
  const streamUrl = root.dataset.stream;
  const contentId = root.dataset.contentId;

  // Runs once per track in one page: clear the previous poll and any stale error styling.
  stopPolling();
  prepare.classList.remove("is-error");
  prepare.querySelector(".spinner").hidden = false;

  const startPlayback = () => {
    prepare.hidden = true;
    transport.classList.remove("is-disabled");

    const audio = activeAudio();

    // Assigning src resets position even for the same URL, and iOS reports a lock-screen wake as
    // "visible" — re-entering here would restart the playing track at 0:00. `ended` does want the reload.
    const alreadyLoaded = loadedContentId === contentId && !audio.ended;
    if (alreadyLoaded && !audio.paused) return;

    if (!alreadyLoaded) {
      const prefetched = pendingObjectUrlFor === contentId ? pendingObjectUrl : null;
      if (prefetched) {
        pendingObjectUrl = null;
        pendingObjectUrlFor = null;
      }
      if (loadedObjectUrl) URL.revokeObjectURL(loadedObjectUrl);
      loadedObjectUrl = prefetched;
      audio.src = prefetched || streamUrl;
      loadedContentId = contentId;
      if (onStart) onStart();
    }

    // Only a fresh load consumes the resume record.
    const resume = alreadyLoaded ? null : consumeResumeState(contentId);
    if (resume) {
      // loadedmetadata never fires again on an already-loaded element.
      if (audio.readyState >= HTMLMediaElement.HAVE_METADATA) audio.currentTime = resume.currentTime;
      else audio.addEventListener("loadedmetadata", () => { audio.currentTime = resume.currentTime; }, { once: true });
    }
    if (!resume || resume.wasPlaying) {
      reportPlayback("play-requested", { contentId, readyState: audio.readyState });
      audio.play().then(
        () => reportPlayback("playing", { contentId }),
        (err) => reportPlayback("play-rejected", { contentId, error: String(err?.name || err) })
      );
      watchPlaybackStarted(contentId);
    } else {
      audio.pause();
    }
  };

  const fail = (message, { permanent = false } = {}) => {
    prepare.hidden = false;
    prepare.classList.add("is-error");
    prepare.querySelector(".spinner").hidden = true;
    prepareText.textContent = message;
    transport.classList.add("is-disabled");
    reportPlayback("prepare-failed", { contentId, message, permanent });
    if (onFail) onFail(message, { permanent });
  };

  if (root.dataset.status === "ready") {
    startPlayback();
    return;
  }

  // The server won't retry it either; fail instantly instead of a wait on a foregone conclusion.
  if (root.dataset.unavailable === "true") {
    fail("Not available on YouTube", { permanent: true });
    return;
  }

  showPreparing();

  if (root.dataset.status !== "downloading") {
    // 409 just means another tab already started it; keep polling either way.
    const { ok, status } = await api(`/content/${contentId}/download`, { method: "POST" });
    if (!ok && status !== 409) {
      fail("Could not start the download");
      return;
    }
  }

  // A missed poll (Wi-Fi blip, throttled background tab) isn't a failed download.
  const MAX_CONSECUTIVE_POLL_FAILURES = 4;
  let consecutiveFailures = 0;

  const checkStatus = async () => {
    if (root.dataset.contentId !== contentId) return;

    const { ok, data } = await api(`/content/${contentId}/status`);
    if (!ok) {
      consecutiveFailures += 1;
      if (consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        stopPolling();
        fail("Lost connection while downloading");
      }
      return;
    }
    consecutiveFailures = 0;

    if (data.status === "ready") {
      stopPolling();
      root.dataset.status = "ready";
      startPlayback();
    } else if (data.status === "error" && data.is_unavailable) {
      stopPolling();
      fail("Not available on YouTube", { permanent: true });
    } else if (data.status === "error") {
      stopPolling();
      // 403: YouTube refused the resolved URL after downloader.py's whole client ladder.
      const refused = data.error_message && /\b403\b|Forbidden/i.test(data.error_message);
      fail(refused ? "YouTube wouldn't serve this track — try again" : "Download failed");
    } else if (data.phase === "converting") {
      prepareText.textContent = "Converting…";
    } else if (data.phase === "downloading") {
      // No length from YouTube: drop the percentage but still update the phase.
      prepareText.textContent =
        data.progress_percent != null ? `Downloading audio… ${data.progress_percent}%` : "Downloading audio…";
    } else if (data.phase === "extracting") {
      // URL resolution (1.4-3s), before any bytes move.
      prepareText.textContent = "Finding audio…";
    }
  };

  const pollToken = {};
  activePollToken = pollToken;
  const startedAt = Date.now();

  const poll = async () => {
    if (activePollToken !== pollToken) return;
    await checkStatus();
    if (activePollToken !== pollToken) return; // checkStatus stopped us, or a newer track took over
    activePollTimer = setTimeout(poll, nextPollDelay(Date.now() - startedAt));
  };
  activePollTimer = setTimeout(poll, nextPollDelay(0));

  // Backgrounded tabs throttle timers; check in immediately on return.
  activeVisibilityHandler = () => {
    if (document.visibilityState === "visible") checkStatus();
  };
  document.addEventListener("visibilitychange", activeVisibilityHandler);
}

/** Beacons visibility transitions while a track is loaded; a lock-screen wake makes no request of its own. */
export function installVisibilityBreadcrumb() {
  document.addEventListener("visibilitychange", () => {
    const contentId = document.getElementById("player-root")?.dataset.contentId;
    if (!contentId) return;
    const audio = activeAudio();
    reportPlayback("visibility-changed", {
      contentId,
      currentTime: Math.round(audio.currentTime),
      paused: audio.paused,
      readyState: audio.readyState,
    });
  });
}

const PLAYBACK_WATCHDOG_MS = 3000;

let playbackWatchdogTimer = null;

/** Reports a play() that neither threw nor started (pending while the page suspends); one retry at most. */
function watchPlaybackStarted(contentId) {
  clearTimeout(playbackWatchdogTimer);
  playbackWatchdogTimer = setTimeout(() => {
    const root = document.getElementById("player-root");
    if (!root || root.dataset.contentId !== contentId) return;
    const audio = activeAudio();
    // Stuck at 0 is the symptom even unpaused: an accepted play() may never produce a frame.
    if (audio.currentTime > 0) return;
    reportPlayback("playback-stalled", { contentId, paused: audio.paused, readyState: audio.readyState });

    // Never load() an unpaused element: it aborts the in-flight play(), which on backgrounded iOS
    // is the whole audio grant. Only a paused element is retried.
    if (!audio.paused) return;
    audio.play().catch((err) => reportPlayback("retry-rejected", { contentId, error: String(err?.name || err) }));
  }, PLAYBACK_WATCHDOG_MS);
}

// Must remove the visibilitychange listener too, or it later restarts a playing track from 0:00.
function stopPolling() {
  activePollToken = null;
  if (activePollTimer) {
    clearTimeout(activePollTimer);
    activePollTimer = null;
  }
  if (activeVisibilityHandler) {
    document.removeEventListener("visibilitychange", activeVisibilityHandler);
    activeVisibilityHandler = null;
  }
}

export function setupFavorite() {
  const btn = document.getElementById("favorite-btn");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    const on = btn.dataset.favorite === "true";
    btn.disabled = true;
    try {
      const { ok, data } = await api(`/content/${btn.dataset.contentId}/favorite`, {
        method: on ? "DELETE" : "POST",
        errorMessage: "Could not update favorite",
      });
      if (!ok) return;

      btn.dataset.favorite = String(data.is_favorite);
      btn.classList.toggle("is-on", data.is_favorite);
      btn.setAttribute("aria-pressed", String(data.is_favorite));
      btn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

      refreshFragments();
    } finally {
      btn.disabled = false;
    }
  });
}
