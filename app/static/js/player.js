// The audio player itself. Drives the in-page overlay (_player_overlay.html,
// which renders _player_controls.html) — its only caller.

import { api, formatDuration, showToast } from "./core.js";
import { refreshFragments } from "./fragments.js";
import { consumeResumeState } from "./resume.js";

const SKIP_SECONDS = 15;

// A download that works settles in roughly two seconds end to end, so a flat
// 1500ms poll — which is what this used to be — spent most of a second of
// dead air after the file was already on disk. Start tight and back off, so
// the common case feels immediate without a genuinely slow download turning
// into a request every 300ms for minutes on end.
// Poll tightly through the window where downloads actually land, then back
// off. Measured end to end, server side: ~2.5s when visionos resolves a URL
// YouTube honours, ~3.7s when the ladder falls through to web_embedded.
//
// The schedule this replaces ramped 250ms up to 2500ms, which put its widest
// gaps exactly where those two land — a 1s gap between 2s and 3s, a 1.5s gap
// between 3s and 4.5s. A file ready at 3.2s wasn't picked up until 4.5s.
// Across the plausible range the average dead air was ~700ms: more than a
// third of the wait was the client simply not asking yet, after the audio was
// already on disk.
//
// 200ms costs 20 requests over four seconds, each a single indexed SQLite
// read on localhost. That is nothing next to the two thirds of a second it
// gives back, and the tight window is bounded so a genuinely slow download
// doesn't keep it up for minutes.
const POLL_TIGHT_MS = 200;
const POLL_TIGHT_UNTIL_MS = 4000;
const POLL_RELAXED_MS = 500;
const POLL_RELAXED_UNTIL_MS = 12000;
const POLL_STEADY_MS = 2000;

// Driven by elapsed time rather than a step counter, so a slow response
// can't shift the whole schedule out from under the window it's aimed at.
//
// Exported because the queue's one-track-ahead prefetch follows its own
// download exactly the same way (see home/overlay.js's cacheUpcoming) and was
// still on a flat 1.5s grid, which is the arrangement the measurements above
// were written to replace. Measured again on a real device on 2026-08-27,
// this time on the prefetch path: 0.95s, 0.99s and 1.69s of dead air on three
// consecutive tracks, against 0.09s on the same session's one track that came
// through the ladder here.
export function nextPollDelay(elapsedMs) {
  if (elapsedMs < POLL_TIGHT_UNTIL_MS) return POLL_TIGHT_MS;
  if (elapsedMs < POLL_RELAXED_UNTIL_MS) return POLL_RELAXED_MS;
  return POLL_STEADY_MS;
}

// There used to be a 3s "stall watchdog" here: if no byte progress had shown
// up by then it POSTed .../download/restart, up to three rounds, and showed
// "(attempt 2 of 3)" in the status text. It was making things worse, not
// better, and the whole mechanism is gone:
//
//   - 3s was shorter than a healthy attempt. Resolving a URL takes 1.4-3s
//     and produces no byte progress at all, so the watchdog fired on
//     downloads that were working.
//   - It couldn't cancel what it abandoned. yt-dlp can't be interrupted, so
//     a restart left the old attempt running and started a second one beside
//     it — two, then three, concurrent yt-dlp runs per play, all writing the
//     same .part file (one play in the logs died on "Unable to rename file").
//   - It threw away successes. The abandoned attempts often finished fine,
//     but a superseded generation's result was discarded on arrival, so the
//     user waited for a later attempt to redo work already done. That is
//     exactly why "the third attempt" appeared to be the one that worked.
//
// Retrying now lives entirely in downloader.py's ladder, which doesn't have
// to guess: it sees the failure itself, in ~1.4s, and moves to the next
// client immediately. This side just polls.

// prepareAudio() can be called repeatedly for different tracks in the same
// page load (switching tracks in the overlay) — this tracks the in-flight
// download-status poll across those calls so a later call can cancel a
// still-running earlier one instead of leaving it to eventually hijack
// playback once its download finishes.
let activePollTimer = null;

// Identifies the prepareAudio call that owns the current poll chain. The
// chain re-arms itself with setTimeout rather than running on a fixed
// setInterval, so "is this still the live track?" has to be checked between
// ticks — a stale chain that kept going would eventually see its own old
// track go ready and hijack playback.
let activePollToken = null;

// Same problem, for the visibilitychange listener prepareAudio registers:
// without tracking and removing the previous call's listener, every track
// that was ever mid-download during this page session leaves a permanent
// zombie handler on document. Later, any visibilitychange fires all of them
// — and a stale one whose track has since finished downloading server-side
// would call its own startPlayback() and hijack the audio element back to
// that old track, regardless of what's actually loaded now.
let activeVisibilityHandler = null;

// Breadcrumbs for the one class of failure the server can't see: playback
// that doesn't happen. A track that never advanced, a download that finished
// but never started playing, a play() the browser refused — from the
// server's side all of those look exactly like "the user stopped listening",
// because nothing gets requested. See routers/debug.py.
//
// sendBeacon rather than fetch: these are posted at precisely the moments a
// mobile browser is most likely to freeze or discard the page, and a beacon
// is handed to the browser to deliver rather than depending on this page
// still running. Failures here are swallowed entirely — diagnostics that can
// break playback are worse than no diagnostics.

// Only these event names actually reach the server — every call site below
// (and in home/overlay.js) still fires unconditionally, on the happy path
// too, but reportPlayback silently drops anything not in this set. Measured
// live: 4 beacons per track played under the old blanket policy ("now-playing",
// "play-requested" and a successful "playing" for starting one, "track-ended"
// for finishing it) — none of those are ever useful for debugging, since
// they're what *every* track produces whether or not anything actually went
// wrong. The four kept here are exactly the ones that only fire when
// something didn't happen the way it should have.
// "track-ended" and "retry-rejected" are here despite the noise budget above.
// Both fire at most once per track, and between them they are the only record
// of *why* playback stopped: track-ended carries `next` and `prepared`
// alongside the page's visibility, which is the difference between "the queue
// was empty" and "the page stopped running" — indistinguishable from the
// server otherwise, and an ambiguity a real investigation got stuck on for
// several rounds before this was added.
// "media-session-action" and "audio-session" earn their place the same way:
// each is a real lock-screen/headset tap or an OS-side interruption, a
// handful per session at most, and they answer the one question the log
// could not answer during the 2026-08-23 investigation — whether a
// lock-screen control that "did nothing" ran our handler and had its play()
// refused, or never ran it at all because iOS had already frozen the page.
// A tap that produces no beacon within a breath of its media event *is* the
// frozen-page case, finally visible from the server.
// "early-handoff" fires at most once per background auto-advance, in place
// of the "track-ended" that advance no longer produces — without it the
// log's per-track story would simply stop wherever the new path takes over.
// This channel has also carried things that are not playback at all, and the
// precedent is worth keeping even though nothing is using it right now: it is
// the only way something only the client can see reaches the server. The
// installed app's bottom-bar placement was settled that way — it reported a
// 932pt screen against an 873pt viewport, which located the missing 59pt at
// the top rather than the bottom and ended several rounds of guessing. The
// same channel then confirmed the fix (a 932pt viewport, the bar ending
// exactly at the glass) and the beacon came out again, which is the shape
// these are meant to have: added for a question, removed with its answer.
// Temporary, for one open question, and two of the four that answered theirs
// have already gone. On 2026-08-27 a track whose prefetch had provably
// completed still started over the network while the track before it, prepared
// the same way, played with no request at all. `handoff-cached` said which:
// the entry was there and ready, and `buffered` was false — the bytes simply
// had not finished arriving. `handoff-missed` separates that from having
// nothing prepared at all, which is what a cold open looks like.
//
// Kept a while longer because they are also how the change that followed gets
// checked: sending for the next track on the open rather than on its first
// timeupdate (see home/overlay.js's prefetchUpcoming) should turn `buffered`
// true where it was false. Remove all three once it has.
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
    // audioSession is Safari-only and experimental; stamped on every beacon
    // (like `visibility`) because the two questions it answers — does this
    // device have the API at all, and was the session "interrupted" at the
    // moment something went wrong — both matter precisely when one of these
    // fires, and a field costs no extra beacons.
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

/**
 * One beacon per lock-screen/headset/notification-shade tap that reaches
 * this page. The element's state is captured *before* the handler acts, so
 * the log shows what the tap found, not what it left behind. Exported for
 * home/overlay.js, whose queue-dependent next/previous handlers live there.
 */
export function reportMediaSessionAction(action) {
  const audio = activeAudio();
  reportPlayback("media-session-action", {
    action,
    contentId: document.getElementById("player-root")?.dataset.contentId,
    paused: audio ? audio.paused : null,
    readyState: audio ? audio.readyState : null,
  });
}


// ---------------------------------------------------------------------------
// Playback element
//
// One <audio> element, its src reassigned per track. Single is meant
// literally: it is the only <audio> in the document, and that is a hard
// constraint rather than a tidiness preference — see below.
//
// Two rounds of work went into making a track that ends while the app is
// backgrounded start the next one, and both were reverted. This comment is
// the record of what was measured, so it isn't re-derived a third time.
//
// **The rule is not "a backgrounded iOS app cannot start a new media
// resource."** That is what the measurements looked like, and it is what the
// two reverted architectures below were built to work around, but it is the
// wrong reading. What actually holds: a backgrounded page may start a new
// resource *for as long as it still holds the audio session*, and the page
// gives that session up the moment it calls pause(). The failure mode is the
// same either way — the element serves real bytes (the server logs 206
// Partial Content) and then stops short of HAVE_FUTURE_DATA, with play()
// neither resolving nor rejecting until the app is foregrounded — so the two
// causes are indistinguishable from the element alone. The way to tell them
// apart is whether anything called pause() first. Nothing on a track switch
// may (see home/overlay.js's openPlayer); closePlayer, which really is done
// with audio, is the only caller that should.
//
// Two things were tried against the wrong reading, in order:
//
//   - **Two interchangeable decks**, pre-rolling the next track on a second
//     element a few seconds early so the handoff never had to start anything.
//     It worked. It also put two elements genuinely playing at once, which
//     Apple's own docs rule out ("all devices running iOS are limited to
//     playback of a single audio or video stream at any time [...] playing
//     multiple simultaneous audio streams is also not supported"), and iOS's
//     Now Playing system stopped being able to tell which element was real:
//     Dynamic Island frozen on the previous track, lock-screen play/pause
//     stuck, audible overlap.
//
//   - **A keep-alive clip** on a second element, looping something inaudible
//     across the gap on the theory that a page which never stops playing
//     keeps its permission to play. It does not. The log has the clip
//     rendering (`keepalive-playing`, hidden) 3 seconds before the real
//     track was still sitting at readyState 1 — permission was refused with
//     something audibly playing on the very same page. All it bought was the
//     same Now Playing ambiguity as the decks, in a smaller package, and it
//     was the reason the lock screen stayed wrong long after the decks were
//     gone.
//
// So: one element, no second stream, and no pause() on a track switch —
// which is enough for background auto-advance on its own, verified on device
// and in the breadcrumb log. Neither deck nor clip is needed, and neither
// should come back; both worked only by accident, because pre-rolling early
// meant the pause()/play() pair happened while the page was still audibly
// playing something, which is what the current code arranges deliberately.
//
// The race that remained after all that has since been closed, the way the
// paragraph this replaces said it would have to be: the element was at
// readyState 0 when the new src was assigned, so every handoff still had to
// fetch the audio over the network at the one moment it could least afford
// to. Prefetching a track meant downloading it to the *server's* disk;
// nothing pulled a byte of it into the page. Measured on a real session
// (9 auto-advances, all of them with the next track already downloaded):
// 0.28-1.43s from `ended` to the /stream request alone, and six
// playback-stalled beacons, every one of them readyState 1 — three seconds
// after play(), metadata in hand and still not one sample of audio.
//
// home/overlay.js's cacheUpcoming now pulls the bytes down during the
// current track and hands this module an object URL for them (see
// offerPrefetchedAudio), so the swap touches no network at all.
// ---------------------------------------------------------------------------

/** The element driving the transport. */
export function activeAudio() {
  return document.getElementById("audio");
}

// Which track the element's current resource belongs to, tracked rather than
// derived. Matching the element's current source against the track's stream
// URL used to answer this and cannot any more: a prefetched track is handed
// a blob: URL, which carries no content id — the comparison would say "not
// loaded" for the track that is playing right now. That answer is load-bearing in two
// places, and getting it wrong is not subtle in either: startPlayback would
// reassign src on a track already playing (which resets it to 0:00 — the
// whole of the screen-wake bug), and home/overlay.js's `ended` handler would
// mistake the finished track for an outgoing one and stop advancing.
let loadedContentId = null;

// The object URL behind that resource, when it came from a prefetch. Held so
// it can be revoked: an object URL pins its Blob in memory until it is, and
// these are whole audio files.
let loadedObjectUrl = null;

// Handed over by openPlayer ahead of the assignment that adopts it, since a
// track that still has to finish downloading gets its src minutes later (or
// never). Ownership transfers with it — this module revokes it whether it
// ends up played or superseded first.
let pendingObjectUrl = null;
let pendingObjectUrlFor = null;

/** The track the element is actually loaded with, or null. */
export function loadedTrackId() {
  return loadedContentId;
}

/**
 * Puts the card into its "working on it" state: spinner up, transport dead.
 *
 * prepareAudio does this for itself, but not until it has the track's
 * metadata — which on a Next press that misses the prefetch is a round trip
 * away, with a live song-version search possibly behind it. Every visible
 * thing went on showing the previous track for that whole time, so the press
 * read as ignored and got pressed again. openPlayer calls this first.
 */
export function showPreparing(message = "Preparing audio…") {
  const prepare = document.getElementById("prepare-state");
  if (!prepare) return;
  prepare.hidden = false;
  prepare.classList.remove("is-error");
  prepare.querySelector(".spinner").hidden = false;
  document.getElementById("prepare-text").textContent = message;
  document.querySelector(".transport")?.classList.add("is-disabled");
}

/** Undoes it, for a track that never got as far as loading. */
export function clearPreparing() {
  const prepare = document.getElementById("prepare-state");
  if (!prepare) return;
  prepare.hidden = true;
  document.querySelector(".transport")?.classList.remove("is-disabled");
}

/**
 * Offers the element bytes for `contentId` that are already in the page (see
 * home/overlay.js's cacheUpcoming), to be used instead of going to the
 * network when this track is started.
 *
 * Called on every open, with a null url when there is nothing prefetched —
 * which is also how a previous offer that was never taken up gets released.
 */
export function offerPrefetchedAudio(contentId, objectUrl = null) {
  if (pendingObjectUrl && pendingObjectUrl !== objectUrl) URL.revokeObjectURL(pendingObjectUrl);
  pendingObjectUrl = objectUrl;
  pendingObjectUrlFor = objectUrl ? String(contentId) : null;
}

/** Drops both, for a player being closed rather than switched. */
export function releaseAudio() {
  offerPrefetchedAudio(null, null);
  if (loadedObjectUrl) URL.revokeObjectURL(loadedObjectUrl);
  loadedObjectUrl = null;
  loadedContentId = null;
}

/** Registers a media listener on the player's audio element. */
export function onPlayerEvent(type, handler) {
  activeAudio()?.addEventListener(type, handler);
}

// A single-sample silent WAV, used only as a throwaway source for
// unlockAudio() below.
const SILENT_AUDIO_DATA_URI =
  "data:audio/wav;base64,UklGRiUAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQEAAACA";

// WebKit (every iOS browser — Apple requires them all to use its engine)
// only allows a script-initiated play() when it directly, synchronously
// results from a user gesture. Our actual playback start happens later —
// after an `await` for track metadata and, for a fresh download, a
// setTimeout-driven status poll — which breaks that chain and gets silently
// blocked, most visibly on iPhone (Chrome's autoplay policy is stickier
// about "the user interacted with this page at some point" and usually
// tolerates the delay fine).
//
// WebKit's unlocked state sticks to the *element*, not the gesture that
// earned it, though: play it once, synchronously, inside a real tap, and
// every later play() call on that same element succeeds without a fresh
// gesture — even after swapping .src out from under it. So do that once,
// on the very first real click anywhere in the page (setupPlayer wires this
// up, below) — deliberately not tied to a specific control (a track row,
// a nav tab, anything) and deliberately not called from openPlayer() itself,
// since openPlayer() also runs from places with no gesture behind them at
// all (a boot-time resume, a remote-triggered open, the "ended" handler's
// auto-advance) — calling it from there risked spending this exactly when
// there was no real gesture to spend, permanently starving every later
// real tap of the one attempt that could have actually unlocked anything.
let audioUnlocked = false;
function unlockAudio() {
  if (audioUnlocked) return;

  const audio = activeAudio();
  // No element, or something is already loaded on it — either nothing to do,
  // or a play() already ran on it some other way. Either way, don't stomp a
  // loaded (possibly playing) source with the silent clip.
  //
  // Returning *without* setting the flag matters: on iOS this page reloads on
  // every bfcache restore (see resume.js), and a reload with something
  // playing runs resumeOverlayIfNeeded -> openPlayer -> startPlayback at boot,
  // which assigns audio.src before the user has clicked anything. Burning the
  // one-shot flag on that first click would mark the element unlocked when
  // nothing had been unlocked at all — and since that boot-time play() has no
  // gesture behind it, WebKit refuses it, so the element really is still
  // locked. Leaving the flag alone costs a no-op call per click and lets a
  // later one (closePlayer clears src) actually do the unlock.
  if (!audio || audio.src) return;

  audioUnlocked = true;
  // What WebKit needs is the *call* happening synchronously inside the
  // click — not that the promise resolves, which is why this doesn't wait
  // on .then() before pausing.
  audio.src = SILENT_AUDIO_DATA_URI;
  audio.play().catch(() => {});
  audio.pause();
}

// Range inputs can't style their "already played" portion natively, so paint it
// with a gradient that tracks the current value.
export function paintRange(input) {
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--fill", `${pct}%`);
}

/**
 * Whether this browser lets a page set the playback volume at all.
 *
 * Feature-detected by writing and reading back rather than sniffed from the
 * user agent: the restriction is per-browser behaviour, not per-OS, and it
 * has moved before. Safe to run at startup because nothing is loaded yet —
 * the value is restored either way.
 */
/**
 * Whether this is an iOS browser — every browser on iOS is WebKit, since
 * Apple requires it.
 *
 * Sniffed from the user agent, which volumeIsSettable below deliberately
 * avoids — and it is here because that feature detection is measurably wrong
 * on a modern iPhone. Confirmed from the device itself (iOS 18.7, Safari
 * 26.6, via a temporary beacon since removed): writing 0.5 and reading it
 * back returns **0.5**, so the detection says "settable" and the slider then
 * does nothing, because the property is not what the output level follows.
 *
 * Apple's own documentation still says the opposite — "the volume property
 * is not settable in JavaScript. Reading the volume property always returns
 * 1" — and that is what this code was originally written against. It has
 * simply stopped being true: the property now keeps what it is given while
 * playback volume stays with the hardware buttons. Nothing readable
 * distinguishes the two cases any more, so this asks who it is talking to
 * instead.
 */
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

  // See unlockAudio above for why this has to be a page-wide "first click,
  // whatever it is" listener rather than something openPlayer() calls.
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

  // These icons are <svg>, i.e. SVGElement — which has no `hidden` IDL
  // property (that lives on HTMLElement). Assigning `.hidden` on them silently
  // creates a plain JS property and never touches the attribute, so CSS
  // `[hidden]` never matches. toggleAttribute() is on Element and works here.
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

  // The ±15s buttons are gone from the transport (shuffle and repeat hold
  // those slots now — see _player_controls.html), but skipping itself is
  // still here on the arrow keys and on the Media Session handlers below.

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
    // unlockAudio() above loads a throwaway silent clip on the page's first
    // click purely to unlock WebKit's autoplay gate — nothing the user asked
    // to hear. A failure there is invisible and harmless by design; without
    // this guard it fires this exact same toast, which reads as "your song
    // won't play" when no real track was ever involved.
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

  // iOS hands playback volume to the hardware buttons: the assignment below
  // is accepted and then has no effect on how loud anything is, so the slider
  // moves and nothing changes. A control that responds to you without doing
  // its job is worse than one that isn't there, so it's removed there. Mute
  // is a separate property and genuinely works, so that button stays.
  //
  // Two gates, because neither is enough alone. The feature detection catches
  // any browser that refuses the write outright. It does *not* catch a modern
  // iPhone, which is the case it was written for: iOS 18.7 returns the value
  // it was handed, so the detection says "settable" over a slider that does
  // nothing. Hence the second, sniffed gate — see isIOSWebKit.
  //
  // **A GainNode was built for this and reverted (2026-08-20).** Routing the
  // element through Web Audio is the usual answer to a volume property that
  // won't take, and it shipped briefly: constructed lazily, on the first
  // slider move below full volume, so a session that never touched the slider
  // kept the untouched playback path.
  //
  // It never actually ran on the phone — selecting it required
  // volumeIsSettable to come back false, and it comes back true — so it
  // proved nothing on device. What settles it is that Apple's own developer
  // forum has the same pair tried together: "I tried the 'volume' property of
  // the <audio> and also the Web Audio API 'GainNode'. Neither approach
  // worked. The player's output stays/reported as 1.0." Both are ignored on
  // iOS; MPVolumeView, which a web page cannot reach, is the only native way.
  //
  // So there is nothing to go back for, and a reason not to: making the gain
  // path selectable means routing iOS playback through Web Audio, where an
  // AudioContext suspended in the background is silence rather than quiet
  // audio — betting the background-playback behaviour that took four rounds
  // to get right (see the Playback element note above) on a volume slider. On
  // iOS the hardware buttons are the volume control, and this stays a mute
  // button.
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

  // Space toggles playback, arrows scrub — as long as focus isn't in a control.
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

/**
 * Publishes whatever the player is currently showing to the OS's Now Playing
 * surface — iOS's lock screen and Dynamic Island, Android's notification
 * shade.
 *
 * Read out of the DOM rather than taking the track as an argument so that the
 * two callers that matter can't disagree: home/overlay.js publishes on a
 * track change (setupMediaSession only ever runs once, at page load, when
 * there is no track yet), and setupMediaSession re-publishes the moment
 * playback actually starts.
 *
 * That second call is not redundant. iOS only reliably accepts a Now Playing
 * update while the page genuinely holds the audio session, and a track change
 * publishes its metadata during the silent gap before playback — the one
 * moment the page holds nothing. Publishing again on `playing` is the same
 * information sent at a moment the OS is guaranteed to take it, which is what
 * makes the Dynamic Island pick up a new track instead of sitting on the
 * previous one.
 */
// What the OS was last told, as a title/artist/artworkSrc key, and whether
// that publish was made at a moment iOS provably held the audio session (a
// `playing` event) — the only kind it is guaranteed to accept rather than
// silently drop. Assigning mediaSession.metadata makes iOS rebuild the whole
// Now Playing card, and applyNowPlayingMetadata runs on *every* `playing`
// event — after each buffering hitch and each resume, not just track changes.
// Re-publishing identical values from those is all cost and no information,
// but ONLY once one held-session publish has landed: the publish a track
// change makes in the silent gap before playback may have been dropped, so
// equality against *that* one proves nothing and must not short-circuit the
// `playing` re-publish that exists to repair it (a Dynamic Island stuck on
// the previous track is the bug that re-publish fixed).
// \u0000-joined because none of the parts can contain NUL, so the key
// cannot collide across field boundaries.
let publishedNowPlaying = null;
let publishedWhileHeld = false;

// The artwork's MIME type, when the URL's extension states it. iOS has
// historically been picky about artwork it isn't told the type of (grey
// squares on the lock screen); an extension is honest evidence, a guess is
// not, so URLs without one just omit the field as before.
const ARTWORK_TYPES = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp" };

export function applyNowPlayingMetadata({ held = false } = {}) {
  if (!("mediaSession" in navigator)) return;
  const title = document.querySelector(".player-title")?.textContent || "";
  const artist = document.querySelector(".player-channel")?.textContent || "";
  const artworkSrc = document.getElementById("player-art-img")?.src || "";

  const key = [title, artist, artworkSrc].join("\u0000");
  if (key === publishedNowPlaying && publishedWhileHeld) return;
  publishedNowPlaying = key;
  publishedWhileHeld = held;

  const extension = artworkSrc.split("?")[0].split(".").pop()?.toLowerCase();
  const type = ARTWORK_TYPES[extension];
  navigator.mediaSession.metadata = new MediaMetadata({
    title,
    artist,
    artwork: artworkSrc ? [type ? { src: artworkSrc, type } : { src: artworkSrc }] : [],
  });
}

/**
 * Takes the app off the OS's Now Playing surface, and forgets what was
 * published so the next applyNowPlayingMetadata can't mistake re-publishing
 * the same track for a redundant update. Both callers are in home/overlay.js:
 * closing the player, and a queue running out entirely — a card left up for
 * audio that is finished is what lingers on the Dynamic Island afterwards,
 * and the page is about to be frozen by iOS, so its controls would be dead
 * anyway. If the track is replayed in-app, the `playing` handler below
 * re-publishes everything.
 */
export function clearNowPlayingMetadata() {
  if (!("mediaSession" in navigator)) return;
  publishedNowPlaying = null;
  publishedWhileHeld = false;
  navigator.mediaSession.metadata = null;
  navigator.mediaSession.playbackState = "none";
}

// Lock-screen/notification-shade transport controls and Bluetooth/headset
// buttons all route through this — without it, playback is only
// controllable while this tab is in the foreground.
function setupMediaSession() {
  if (!("mediaSession" in navigator)) return;

  applyNowPlayingMetadata();

  if ("audioSession" in navigator) {
    // WebKit's Audio Session API (Safari-only, experimental): "playback"
    // declares this page a media player — the category iOS keeps running
    // with the screen locked and doesn't mute under the ring/silent switch —
    // instead of leaving the OS to infer it per play(). The default ("auto")
    // mostly infers correctly, which is why audio worked before this line;
    // declaring it is about the edges the 2026-08-23/24 audit chased, where
    // iOS decides what the page deserves at moments nothing is rendering
    // (the gap after `ended`, a paused session it is about to freeze).
    // Wrapped because an implementation is free to refuse the assignment.
    try {
      navigator.audioSession.type = "playback";
    } catch (err) {
      /* Stays "auto" — exactly what every non-Safari browser does anyway. */
    }

    // OS interruptions (a phone call, Siri, another app taking the output)
    // are otherwise invisible in the log: from the server they look
    // identical to the user pausing.
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

  // Whether audio is genuinely coming out of the element, as opposed to
  // having been asked for.
  //
  // `audio.paused` answers the wrong question. play() flips it to false the
  // instant it is called, and a backgrounded iOS app is free to accept that
  // call and then never render a thing — the refusal is silent, the promise
  // neither resolves nor rejects. Driving the lock screen off `paused` is
  // therefore how it ends up showing a Pause button, and an elapsed-time
  // clock ticking forward, over total silence. Only `playing` means sound.
  //
  // Deliberately not cleared on `waiting`: an ordinary buffering hitch would
  // otherwise flap the lock screen between states mid-track.
  let rendering = false;
  const setRendering = (on) => {
    rendering = on;
    navigator.mediaSession.playbackState = on ? "playing" : "paused";
  };

  const syncPositionState = () => {
    // Nothing rendering, nothing reported — deliberately, and *explicitly*.
    // The OS extrapolates the lock screen's elapsed-time clock locally from
    // the last playbackRate it was handed, on its own clock, independent of
    // playbackState — so reporting position for a track iOS is silently
    // refusing to start (loadedmetadata fires even then) would set that
    // clock ticking over silence. This used to be attempted by switching the
    // reported rate to zero for a silent element, but the spec makes a rate
    // of zero a TypeError — paused is playbackState's job — so the call
    // always threw, the catch below always swallowed it, and "no update at
    // all" was what actually shipped. Same outcome, now stated instead of
    // stumbled into:
    // position state is published only from moments audio is really coming
    // out, and the last published state simply stands while it isn't.
    if (!rendering) return;
    const audio = activeAudio();
    // Number.isFinite, not a NaN check: a resource without a determinable
    // end (duration Infinity) is a state setPositionState throws on too.
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    try {
      navigator.mediaSession.setPositionState({
        duration: audio.duration,
        // Clamped: mid-seek the two readings can momentarily cross, and
        // position > duration is a TypeError rather than a correction.
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

  // `play` fires when playback is *asked for*; `playing` fires when audio is
  // actually coming out. Only the second one is a moment iOS is holding the
  // audio session, so it's the only moment a Now Playing update is certain to
  // be accepted — see applyNowPlayingMetadata. Everything the OS shows gets
  // re-asserted here together, because a track change publishes all of it
  // during the silent gap beforehand, where any of it may have been dropped.
  onPlayerEvent("playing", () => {
    setRendering(true);
    // held: this is the one moment iOS is guaranteed to accept the publish —
    // it marks the published state as trustworthy, which is what lets later
    // `playing` events (buffering hitches, resumes) skip the re-publish.
    applyNowPlayingMetadata({ held: true });
    syncPositionState();
    // The one thing the log couldn't previously settle: whether a lock screen
    // showing the wrong control is this side getting the state wrong, or iOS
    // ignoring a state this side had right all along.
    reportPlayback("now-playing", {
      contentId: document.getElementById("player-root")?.dataset.contentId,
      playbackState: navigator.mediaSession.playbackState,
      paused: activeAudio().paused,
    });
  });
}

// Browsers speculatively load links (prerender runs the page's JS) and a
// ctrl/cmd-clicked track can open a genuinely backgrounded new tab — either
// way, the page's JS runs before the user has actually looked at it. Since
// opening a track is what triggers a download, calling prepareAudio()
// without this guard would let mere prefetching or an unfocused background
// tab fill the user's disk. home/overlay.js's openPlayer() is the only
// caller now that player.html (which had its own page-load call to guard)
// is gone — wrapping there covers a real click just as harmlessly (the page
// is already visible by then, so this resolves immediately) as it covers
// the boot-time resume/deep-link paths that don't involve a click at all.
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
 * Downloads are triggered by playing something, not by a separate button on
 * the card. If the audio isn't on disk yet, kick off the download here and
 * hold the transport disabled until the file is ready.
 *
 * onStart (optional) fires exactly once, right as this track becomes the one
 * loaded in the audio element.
 *
 * onFail (optional) fires every time this ends in the disabled "Download
 * failed" state — a track pulled from a queue (e.g. one YouTube has since
 * made private) shouldn't just die there while the app is backgrounded; the
 * overlay uses this to skip on to whatever's next instead. The inline error
 * still gets drawn regardless, since a track opened with nothing queued
 * after it (or opened directly, not through a queue) has nowhere to skip to.
 * It receives `{ permanent }`: a track YouTube won't serve to anyone (see
 * Content.is_unavailable) is a settled fact about that one track, whereas
 * any other failure might be a sign that YouTube is refusing us in general —
 * and the overlay's skip limits treat those two very differently.
 */
export async function prepareAudio(onStart, onFail) {
  const root = document.getElementById("player-root");
  const prepare = document.getElementById("prepare-state");
  const prepareText = document.getElementById("prepare-text");
  const transport = document.querySelector(".transport");
  const streamUrl = root.dataset.stream;
  const contentId = root.dataset.contentId;

  // Both only matter because this can run more than once per page load — the
  // overlay calls it again for each new track. Without clearing the
  // previous call's poll, a still-downloading earlier track can finish later
  // and hijack playback out from under whatever's loaded now. Without
  // resetting the error styling, a track opened after an earlier one failed
  // would inherit its stale "Download failed" look.
  stopPolling();
  prepare.classList.remove("is-error");
  prepare.querySelector(".spinner").hidden = false;

  const startPlayback = () => {
    prepare.hidden = true;
    transport.classList.remove("is-disabled");

    const audio = activeAudio();

    // Assigning src runs the media element's load algorithm, and that resets
    // the playback position to the beginning — even when the URL assigned is
    // the one already loaded. So a second call here for the track that is
    // already playing does not "make sure it's playing", it silently restarts
    // it from 0:00.
    //
    // Which is not hypothetical. iOS reports a page as visible again when the
    // screen merely *wakes* at the lock screen, without being unlocked, and
    // that re-runs the visibility check-in registered at the bottom of this
    // function. On a real session it re-entered here for the track already
    // playing, reset it to 0:00, and — the phone still being locked — left it
    // pinned there: "playing" on the lock screen, never advancing. The track
    // never reached its end, so it never advanced to the next one either, and
    // no watchdog saw it (see watchPlaybackStarted). Playback stopping is
    // also what killed any chance of recovery: a home-screen PWA stays awake
    // only while sound is actually coming out of it, so iOS froze the app
    // moments later and nothing ran again until the user unlocked the phone,
    // at which point the pending play() finally took and it resumed from 0:00.
    //
    // `ended` is excluded on purpose: replaying a track that has run out is a
    // real restart, and does want the reload.
    const alreadyLoaded = loadedContentId === contentId && !audio.ended;
    if (alreadyLoaded && !audio.paused) return;

    if (!alreadyLoaded) {
      // The bytes, if the previous track pulled them down for us; the URL to
      // go and get them otherwise. Only the former makes a handoff free —
      // the latter is a network fetch starting at the exact moment the
      // listener is waiting on silence.
      const prefetched = pendingObjectUrlFor === contentId ? pendingObjectUrl : null;
      if (prefetched) {
        pendingObjectUrl = null;
        pendingObjectUrlFor = null;
      }
      // The outgoing track's Blob, now that its resource is being replaced.
      if (loadedObjectUrl) URL.revokeObjectURL(loadedObjectUrl);
      loadedObjectUrl = prefetched;
      audio.src = prefetched || streamUrl;
      loadedContentId = contentId;
      if (onStart) onStart();
    }

    // Only a fresh load can be seeked into: consuming the record for a track
    // that is already loaded would spend it on a seek it doesn't need.
    const resume = alreadyLoaded ? null : consumeResumeState(contentId);
    if (resume) {
      // Waiting for loadedmetadata on an element that has already loaded
      // would wait forever, so a ready one is seeked outright.
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

  // YouTube has already refused this one to every client there is, and the
  // server won't attempt it again either (see routers/content.py's
  // start_download). Answering from what we already know turns a four-second
  // wait on a foregone conclusion into an instant skip — which matters most
  // in a queue, where this is otherwise a stall the user is not there to see.
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

  // The download itself is a server-side background task, independent of
  // whether this tab can currently reach the server — a single missed poll
  // (a Wi-Fi blip, a backgrounded mobile tab getting its timers/network
  // throttled, a momentary server hiccup) doesn't mean the download failed,
  // just that this one check-in did. Only give up after several consecutive
  // misses; a lone one is silently retried on the next tick.
  const MAX_CONSECUTIVE_POLL_FAILURES = 4;
  let consecutiveFailures = 0;

  const checkStatus = async () => {
    // This track may no longer be the one loaded (a later openPlayer() call
    // superseded it) even if this call's timer/listener somehow still fired —
    // belt-and-suspenders alongside stopPolling above.
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
      // A 403 here means YouTube resolved a media URL and then refused it.
      // By the time this shows, downloader.py has already been through every
      // rung of its ladder — visionos twice, then web_embedded — so trying
      // again right now is worth a shot but not something to promise.
      const refused = data.error_message && /\b403\b|Forbidden/i.test(data.error_message);
      fail(refused ? "YouTube wouldn't serve this track — try again" : "Download failed");
    } else if (data.phase === "converting") {
      prepareText.textContent = "Converting…";
    } else if (data.phase === "downloading") {
      // The percentage is absent when YouTube serves no length to divide by,
      // which is a reason to drop the number — not to say nothing and leave
      // the text on the previous phase.
      prepareText.textContent =
        data.progress_percent != null ? `Downloading audio… ${data.progress_percent}%` : "Downloading audio…";
    } else if (data.phase === "extracting") {
      // The 1.4-3s where the server is resolving a URL YouTube will honour.
      // No bytes move yet, so without this the text would sit on "Preparing
      // audio…" for the entire slowest part of a play.
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

  // Mobile browsers throttle/suspend timers for a backgrounded tab, so the
  // interval above may not have ticked in a while by the time the user
  // switches back — check in immediately instead of waiting for the next
  // scheduled tick.
  activeVisibilityHandler = () => {
    if (document.visibilityState === "visible") checkStatus();
  };
  document.addEventListener("visibilitychange", activeVisibilityHandler);
}

/**
 * Records what the player was doing whenever the page changes visibility.
 *
 * The one thing about a locked phone the server cannot otherwise see is the
 * moment its screen came on: it produces no request of its own, so a report
 * like "it advances with the screen off but not while the screen is awake on
 * the lock screen" is invisible in the log — there is nothing to line the
 * failure up against. reportPlayback already stamps `visibility` on every
 * beacon; this is the beacon for the transition itself.
 *
 * Only while a track is loaded, so it stays quiet outside playback.
 */
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

// How long to give a play() call before concluding it didn't take. Long
// enough to cover a slow first byte off disk, short enough that a listener
// isn't left in silence wondering.
const PLAYBACK_WATCHDOG_MS = 3000;

let playbackWatchdogTimer = null;

/**
 * Last line of defence for a play() that neither threw nor started.
 *
 * The promise rejecting is the documented way a blocked play() reports
 * itself, and the paths above handle that — but it isn't the only way to
 * end up silent. A play() issued while the browser is in the middle of
 * suspending the page can be left pending indefinitely, resolving only once
 * the page is looked at again, and there is no event for that. This notices
 * (and says so, in the log) rather than leaving it to be reported as "it
 * just stopped".
 *
 * One retry, not a loop: if a second attempt three seconds later also
 * doesn't take, the cause isn't something retrying will fix, and the
 * transport is right there.
 */
function watchPlaybackStarted(contentId) {
  clearTimeout(playbackWatchdogTimer);
  playbackWatchdogTimer = setTimeout(() => {
    const root = document.getElementById("player-root");
    // A different track since then, or it's playing — either way, done here.
    if (!root || root.dataset.contentId !== contentId) return;
    const audio = activeAudio();
    // Still pinned at the start is the symptom, paused or not. Bailing out on
    // `!audio.paused` let through the worse of the two states: a play() the
    // browser accepted and then never produced a single frame for leaves the
    // element unpaused at 0 indefinitely — "playing" on the lock screen with
    // nothing coming out. A real session died in exactly that state and this
    // was the only thing that could have noticed.
    if (audio.currentTime > 0) return;
    reportPlayback("playback-stalled", { contentId, paused: audio.paused, readyState: audio.readyState });

    // Reported, and for an unpaused element deliberately not repaired.
    //
    // `readyState: 1` on an element that is not paused does not mean stuck,
    // it means "no data yet" — a backgrounded PWA opening audio sits there
    // for seconds as a matter of course, with a play() already in flight
    // waiting on it. A version of this called audio.load() to unstick it.
    // That aborts the in-flight play() (AbortError, measured) and throws away
    // the buffering already done, and on a real device tracks died within
    // seconds of the call: three stalls, three AbortErrors, and the last one
    // never played again. On iOS the pending play() of a backgrounded page is
    // the whole audio grant — interrupting a load is worse than waiting for
    // one.
    //
    // A paused element is the case where there is nothing in flight to
    // destroy, and retrying it is what this was written for.
    if (!audio.paused) return;
    audio.play().catch((err) => reportPlayback("retry-rejected", { contentId, error: String(err?.name || err) }));
  }, PLAYBACK_WATCHDOG_MS);
}

// Stops both the poll and the visibilitychange check-in — anything that ends
// a track's polling (ready, error, or a new track superseding it) needs both
// gone. Leaving the visibilitychange listener behind after the timer is
// cleared turns it into a zombie: the next foreground/background cycle would
// still fire it, see "ready" again, and call startPlayback() a second time —
// restarting a track that was already playing fine from 0:00.
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
