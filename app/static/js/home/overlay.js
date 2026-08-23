// The in-page player: a full "now playing" overlay plus a mini bar that stays
// visible while it's collapsed. Every surface opens tracks through this —
// Home's shelves here, the channel/playlist detail panel and Explore's
// results from home/detail.js and home/explore.js respectively — since
// there's no longer a separate standalone player page for any of them to
// navigate to instead.
//
// player.js's setupPlayer/prepareAudio/setupMediaSession/setupFavorite run
// against this markup unmodified (it renders the same _player_controls.html
// partial); everything here is the glue specific to reusing that DOM across
// several tracks in one page load instead of once per load.

import { api, formatDuration, showToast } from "../core.js";
import { refreshFragments, refreshQueuePanel } from "../fragments.js";
import {
  activeAudio,
  applyNowPlayingMetadata,
  clearPreparing,
  loadedTrackId,
  offerPrefetchedAudio,
  onPlayerEvent,
  paintRange,
  prepareAudio,
  releaseAudio,
  reportMediaSessionAction,
  reportPlayback,
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

// Arrow-key step on the mini bar's progress slider. Matches player.js's
// SKIP_SECONDS so scrubbing feels the same wherever the focus happens to be.
const SEEK_STEP_SECONDS = 15;

// How the prefetch follows its own download to completion, so the handoff
// knows whether the next track is actually playable before it gets there
// (see cacheUpcoming). Bounded well inside the length of a track — a
// download that hasn't landed by then won't be helped by asking again, and
// the handoff falls back to preparing it the ordinary way.
const UPCOMING_POLL_MS = 1500;
const UPCOMING_POLL_LIMIT = 20;

// Ceiling on what a prefetch will hold in memory as a Blob. The library's
// tracks run about 1.3 MB each (audio-only m4a at the bitrate this app asks
// YouTube for), so this is well over an order of magnitude of headroom and
// only ever declines something anomalous — an hour-long upload that happened
// to land in a queue. Those still play, just over the network like before.
const PREFETCH_MAX_BYTES = 24 * 1024 * 1024;

/**
 * The next track, fetched while the current one is still playing: its
 * metadata, its download status as of the last check, and — once the
 * download has landed — an object URL for its audio, already in memory.
 *
 * This exists so that a track ending doesn't have to ask the server anything
 * before it can start the next one. `ended` fires, and everything from there
 * to audio.play() — the queue pointer, the dataset, the artwork, the play
 * call itself — can run synchronously inside that one event, with no fetch
 * in the middle for a browser that's busy suspending the page to defer
 * indefinitely. That deferral is exactly what "it didn't move to the next
 * song until I opened the app again" was.
 */
let upcomingTrack = null;

// Caps openPlayer's auto-skip-on-failure (below) at this many failures in a
// row before it gives up instead of trying yet another track. Without a
// cap, a systemic hiccup — YouTube rate-limiting/bot-checking the IP, or
// deciding a whole player client is no longer welcome — reads as "every
// remaining track in the queue is broken" and the skip chain burns through
// all of them in seconds, each one running its own multi-attempt ladder against
// YouTube. That volume is itself what trips the bot check in the first
// place, which is how one bad track once took out an entire session: every
// track after it failed with the exact same "Sign in to confirm you're not
// a bot" error in under two minutes, not because they were all actually
// unavailable.
const MAX_CONSECUTIVE_AUTO_SKIPS = 3;
let consecutiveAutoSkipFailures = 0;

// A track YouTube has settled on refusing (see Content.is_unavailable) is a
// different thing entirely: skipping it costs one local lookup, hits YouTube
// zero times, and says nothing at all about whether the next one will work —
// so the reasoning behind the cap above simply doesn't apply. It gets a much
// looser limit of its own, there only so that a queue of nothing but
// unavailable tracks terminates rather than racing to the end of the list.
const MAX_CONSECUTIVE_UNAVAILABLE_SKIPS = 10;
let consecutiveUnavailableSkips = 0;

/**
 * "Open this artist's page", announced rather than called.
 *
 * home/detail.js is what owns openDetail, and it already imports this module
 * for openPlayer — importing it back would be a cycle. Same one-way
 * arrangement home/remote.js uses for ARTIST_FOLLOWED and home/queue.js for
 * QUEUE_CHANGED: the module that knows *that* something should open says so,
 * and the module that knows *how* listens.
 *
 * detail is `{ pageId }` — see ContentOut.artist_page_id.
 */
export const OPEN_ARTIST = "spotea:open-artist";

function expandPlayer() {
  document.getElementById("player-overlay").hidden = false;
}

function collapsePlayer() {
  // Shut behind us: the overlay is the full-size player, and coming back to
  // it half-collapsed under a queue nobody asked to reopen is a state the
  // user never chose.
  setQueueOpen(false);
  document.getElementById("player-overlay").hidden = true;
}

function syncMiniPlayerInfo(data) {
  document.getElementById("mini-player-title").textContent = data.title;
  document.getElementById("mini-player-channel").textContent = data.channel_title || "";
  const img = document.getElementById("mini-player-art-img");
  if (data.thumbnail_url) {
    img.src = data.thumbnail_url;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

export async function openPlayer(contentId, { expanded = true, requireVisible = true } = {}) {
  contentId = String(contentId);
  const root = document.getElementById("player-root");

  // Every route into the player lands here, so this is the one place that can
  // keep the queue pointer honest — including the routes that have nothing to
  // do with a queue (a Home shelf, an Explore result), which is exactly when
  // the queue has to be dropped rather than left to advance into a list the
  // user has moved on from. See queue.js's noteCurrent.
  noteCurrent(contentId);

  if (root.dataset.contentId === contentId) {
    // Same track already loaded — just surface it, don't touch playback.
    expandPlayer();
    return;
  }

  // Nothing is stopped here, deliberately. Switching tracks used to open with
  // audio.pause() so the outgoing track didn't play on underneath the new
  // one's "Preparing audio…" state — first unconditionally, then guarded to
  // skip the case where the element had already run out on its own.
  //
  // Both versions were wrong in the same way, and the guard only hid it on
  // the auto-advance path. pause() tells iOS the page is done with audio,
  // which closes the background-audio grant that lets a backgrounded page
  // start anything at all; every later play() is then silently ignored until
  // the app is foregrounded. On an auto-advance the element is already paused
  // so the guard skipped the call and that path worked — but a lock-screen
  // next tap arrives with the current track genuinely playing, so the guard
  // let it through and that path never worked once. Breadcrumbs from a real
  // device (see reportPlayback): tap, pause, then a 6.7s download, then
  // play-requested from a setTimeout that iOS had already stopped listening
  // to, then `playing` only on the next visibilitychange.
  //
  // Assigning audio.src in startPlayback interrupts the outgoing resource by
  // itself, without ever telling the OS the page is finished with sound — so
  // the element holds the audio session continuously across the swap, which
  // is the one state in which iOS accepts a new resource off screen. The cost
  // is that a track needing a download plays the outgoing one for those few
  // seconds instead of cutting to silence, which is the better of the two.
  //
  // The progress UI is reset from prepareAudio's onStart below rather than
  // here, so it changes when the audio changes: zeroing it here would blank
  // the bar for a track that is still audibly playing.

  // Whether there is a card on screen already, for the instant-feedback
  // branch below.
  const wasOpen = Boolean(root.dataset.contentId);

  // Taken, not read: the cached copy is only good for the one handoff it was
  // fetched for, and leaving it in place would let a later, unrelated open of
  // the same track run on however stale it had become by then.
  let data = null;
  let prefetchedAudio = null;
  if (upcomingTrack && upcomingTrack.id === contentId) {
    data = upcomingTrack.data;
    prefetchedAudio = upcomingTrack.objectUrl;
    reportPlayback("handoff-cached", { contentId, status: data.status, buffered: Boolean(prefetchedAudio) });
  } else if (upcomingTrack?.objectUrl) {
    // Bytes pulled down for a track this open isn't going to. Nothing will
    // ever read them, and an object URL pins its Blob until revoked.
    URL.revokeObjectURL(upcomingTrack.objectUrl);
  }
  upcomingTrack = null;
  // Ownership passes to player.js, which adopts this at the src assignment
  // and revokes it whether or not it gets that far. Called unconditionally,
  // null included: that is also what releases an offer made for a track the
  // user moved off before it finished preparing.
  offerPrefetchedAudio(contentId, prefetchedAudio);

  // Only when there was nothing prepared — this is the await the cache
  // exists to avoid, and reaching it is fine, just slower.
  if (!data) {
    // Say so now rather than after the round trip. Only with a track already
    // open: this puts the *card* into its preparing state, and a cold first
    // open has no card on screen to put anywhere — surfacing an empty one
    // that may yet fail to load would be worse than the wait it covers.
    if (wasOpen) showPreparing();

    const res = await api(`/content/${contentId}`);
    if (!res.ok) {
      showToast("Could not load this track");
      // Nothing is going to load, so the spinner above has to come back off —
      // the transport stays disabled otherwise and the track that is still
      // playing can't be paused.
      if (wasOpen) clearPreparing();
      // If this call came from resumeOverlayIfNeeded, the sessionStorage
      // record it read is exactly what just failed to load (e.g. the row was
      // deleted since) — consumeResumeState
      // only ever clears it on a *successful* startPlayback, so without this
      // a permanently invalid record would re-trigger this same failure on
      // every page load.
      clearResumeState();
      return;
    }
    data = res.data;
    data = await songVersionOf(data);
  }

  document.querySelector(".player-title").textContent = data.title;
  const channelBtn = document.querySelector(".player-channel");
  channelBtn.textContent = data.channel_title || "";
  channelBtn.dataset.artistPageId = data.artist_page_id || "";
  // Disabled rather than left as a dead control: a track whose artist row was
  // never resolved past the placeholder it was created from has no page to
  // open, and the line styles itself back into plain text (see style.css).
  channelBtn.disabled = !data.artist_page_id;
  const artImg = document.getElementById("player-art-img");
  if (data.thumbnail_url) {
    artImg.src = data.thumbnail_url;
    artImg.hidden = false;
  } else {
    artImg.removeAttribute("src");
    artImg.hidden = true;
  }
  document.getElementById("duration-time").textContent = data.duration_seconds
    ? formatDuration(data.duration_seconds)
    : "0:00";

  const favBtn = document.getElementById("favorite-btn");
  favBtn.dataset.contentId = data.id;
  favBtn.dataset.favorite = String(data.is_favorite);
  favBtn.classList.toggle("is-on", data.is_favorite);
  favBtn.setAttribute("aria-pressed", String(data.is_favorite));
  favBtn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

  root.dataset.contentId = String(data.id);
  root.dataset.status = data.status;
  root.dataset.unavailable = String(data.is_unavailable === true);
  root.dataset.stream = `/content/${data.id}/stream`;

  syncMiniPlayerInfo(data);

  // setupMediaSession (player.js) only reads the DOM once, at page-load time
  // — on index.html that's before any track has ever been opened, so it can't
  // be what keeps lock-screen/notification metadata current across repeated
  // openPlayer() calls. This has to do it explicitly, every time. It runs
  // after the writes above because it reads the same DOM they just filled in.
  //
  // This publish happens in the silent gap before playback, which iOS may
  // simply drop; player.js re-publishes on `playing` for that reason. Both
  // are needed — the OS has to be told before the track starts (so the lock
  // screen isn't briefly showing the previous one) and again once it has.
  applyNowPlayingMetadata();

  // The mini bar always surfaces — only whether the full "now playing" view
  // is what's on top depends on the caller (resumeOverlayIfNeeded passes
  // expanded: false to put a track back exactly how it was left).
  document.getElementById("player-overlay").hidden = !expanded;
  document.getElementById("mini-player").hidden = false;
  document.body.classList.add("has-mini-player");

  const start = () => {
    prepareAudio(
      () => {
        // onStart fires just after audio.src is reassigned, i.e. the moment
        // the outgoing track actually stops being what's playing — see the
        // note in openPlayer above for why this can't be done up front.
        const seekBar = document.getElementById("seek-bar");
        seekBar.value = 0;
        document.getElementById("current-time").textContent = "0:00";
        paintRange(seekBar);
        document.getElementById("mini-player-progress-fill").style.width = "0%";

        // The play used to be recorded as a side effect of the /stream
        // request the src assignment kicked off, and the shelves were
        // refreshed on loadedmetadata because that reliably came after it.
        // Neither holds now: a prefetched track makes no request here at all
        // (its bytes are already in the page) and the one it did make
        // happened a whole track ago. So the play is stated outright, and
        // the refresh waits on that rather than on a media event that no
        // longer implies the server knows anything.
        api(`/content/${data.id}/played`, { method: "POST" }).then(() => refreshFragments());
        consecutiveAutoSkipFailures = 0;
        consecutiveUnavailableSkips = 0;
      },
      (message, { permanent } = {}) => {
        // A queued track that fails to download (pulled from YouTube, gone
        // private, ...) used to just leave the transport stuck on "Download
        // failed" — most noticeably when this happens while the app is
        // backgrounded, since auto-advance into it is exactly how a
        // background "ended" handoff (below) reaches a broken track with no
        // one watching to hit "next". Skip past it instead, same as the
        // queue running out normally does nothing when there's no next id.
        //
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
          // See MAX_CONSECUTIVE_AUTO_SKIPS above — leave this one showing its
          // real error rather than trying yet another track.
          showToast("Several tracks in a row failed — stopping instead of skipping further");
          return;
        }
        showToast(`Couldn't play "${data.title}" — skipping to the next track`);
        playFromQueue(nextId());
      }
    );
  };

  // Waits out a prerender or a ctrl/cmd-clicked background tab (see
  // player.js's whenVisible) — resolves immediately for a real click, since
  // the page is already visible by then. Skipped entirely for a queue
  // handoff (requireVisible: false, set by playFromQueue below): a locked
  // screen or a backgrounded tab is `document.visibilityState !== "visible"`
  // exactly like an unopened prerender is, so without this every "ended" and
  // every lock-screen/headset next/previous tap while the app isn't on
  // screen would queue up behind a visibilitychange that only fires once the
  // user looks at the phone again — audio would just stop instead of
  // advancing. That's safe to skip here specifically because a queue handoff
  // can only happen after some earlier track already made it through this
  // same gate once for real (there is no queue, no "ended" event, and no
  // media-session next/previous handler until real playback has begun).
  if (requireVisible) whenVisible(start);
  else start();
}

/**
 * Swaps a music-video row for the song it is a video of, if it is one.
 *
 * Explore's playlists are video playlists almost end to end, and a video
 * entry is the worse copy of the track in every way that shows: a 16:9 still
 * where the rest of the app draws square album art, no lyrics, and a
 * recording with an intro on it. See routers/content.py's
 * swap_in_song_version for what the server does and why it rewrites the row
 * rather than adding one.
 *
 * One request, and only for the track actually being played — a playlist is
 * fifty of these and resolving all of them on open would be fifty live
 * searches for a list most of which nobody will hear. The row is rewritten,
 * so the second play of the same track costs nothing.
 *
 * Best effort in both directions: a failed call, or a track with no song
 * version, hands back exactly what it was given.
 */
async function songVersionOf(data) {
  if (!data.is_music_video) return data;
  const { ok, data: resolved } = await api(`/content/${data.id}/song-version`, { method: "POST" });
  return ok && resolved ? resolved : data;
}

/**
 * Pulls the next track down ahead of time and caches its metadata in
 * `upcomingTrack`, so the handoff to it doesn't have to wait on either.
 *
 * The download POST is the reason this exists at all: without it every track
 * change in a queue costs the same "Preparing audio…" wait as the first one.
 * The metadata fetch beside it and the follow-up polling are what let the
 * handoff skip its own `/content/{id}` round trip too.
 *
 * Runs while the current track is still playing, which is what makes the
 * polling affordable and reliable: the page is awake by definition, each
 * check is one indexed read on localhost, and it stops as soon as there's a
 * settled answer. Everything here is best-effort — a failure just means the
 * handoff prepares the track the ordinary way instead.
 */
async function cacheUpcoming(contentId) {
  const id = String(contentId);
  // The song swap runs *before* the download, not beside it: the row's
  // video_id is what gets fetched, and the server refuses to rewrite it once
  // a file exists (see swap_in_song_version). Resolving first means the
  // prefetch pulls down the song rather than the video, and the handoff
  // finds it already on disk.
  const meta = await api(`/content/${id}`);
  if (!meta.ok) return;
  const resolved = await songVersionOf(meta.data);

  // Published the moment it exists, rather than after the download POST
  // below. Everything above this line is exactly what openPlayer would
  // otherwise have to do for itself — and songVersionOf is a live search, the
  // slowest thing on the whole path — so a Next press landing in this window
  // used to find nothing here and repeat all of it before a single thing on
  // screen changed. Now it finds the metadata and only the audio is still
  // outstanding.
  upcomingTrack = { id, data: { ...resolved }, objectUrl: null };

  // The metadata and the swap used to run beside the download in one
  // Promise.all. They can't any more: the download fetches whatever
  // video_id the row currently names, so the swap has to have landed first
  // or the prefetch pulls down the music video and the handoff arrives to
  // find the wrong file already on disk.
  const download = await api(`/content/${id}/download`, { method: "POST" });
  // Taken by the handoff while that was in flight — this entry is somebody
  // else's now (or nobody's), and writing to it would resurrect a cache for
  // the track that is already playing.
  if (upcomingTrack?.id !== id) return;

  // The POST's answer is the more recent of the two, and the only one that
  // can say "already on disk" for a track that needed no download at all.
  const data = upcomingTrack.data;
  if (download.ok && download.data) {
    data.status = download.data.status;
    data.is_unavailable = download.data.is_unavailable === true;
  }
  if (data.is_unavailable || data.status === "error") return;
  if (data.status === "ready") {
    await cacheUpcomingAudio(id);
    return;
  }

  for (let attempt = 0; attempt < UPCOMING_POLL_LIMIT; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, UPCOMING_POLL_MS));
    // Superseded (the queue moved on, or the handoff already took this) —
    // whatever comes back now belongs to nothing.
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

/**
 * The half of the prefetch that actually buys the handoff anything: the
 * audio itself, pulled into the page while the current track still plays.
 *
 * Downloading the next track server-side only ever moved the wait — the
 * element still had to fetch the file over the network at the one instant
 * the listener is sitting in silence. Measured across nine auto-advances
 * that all had their next track already on the server's disk: 0.28-1.43s
 * just to get the /stream request out, and six readyState-1 stalls three
 * seconds after play(). With the bytes already here the swap is a src
 * assignment against memory (see player.js's offerPrefetchedAudio).
 *
 * Best effort from end to end: anything that goes wrong leaves objectUrl
 * null and the handoff goes over the network exactly as it used to.
 */
async function cacheUpcomingAudio(id) {
  let objectUrl = null;
  try {
    // Plain /stream, no marker of its own: asking for it no longer records a
    // play (see routers/content.py's stream_content), which is precisely
    // what makes fetching a track early safe to do.
    const res = await fetch(`/content/${id}/stream`);
    if (!res.ok) return;
    // Declined before buffering when the server says how big it is, and
    // again afterwards for the case where it didn't.
    const declared = Number(res.headers.get("content-length"));
    if (Number.isFinite(declared) && declared > PREFETCH_MAX_BYTES) return;
    const blob = await res.blob();
    if (blob.size > PREFETCH_MAX_BYTES) return;
    objectUrl = URL.createObjectURL(blob);
  } catch (err) {
    return;
  }

  // Superseded while the bytes were in flight — the handoff has already been
  // and gone, or the queue moved somewhere else. Nothing will read these.
  if (upcomingTrack?.id !== id) {
    URL.revokeObjectURL(objectUrl);
    return;
  }
  upcomingTrack.objectUrl = objectUrl;
}

/**
 * Opens a track the queue handed us, keeping the overlay however the user
 * left it. A fixed `expanded: true` would be right for a tapped next button
 * and wrong for everything else — auto-advance and the lock-screen/headset
 * controls both fire while the app is collapsed to the mini bar or not on
 * screen at all, and throwing the full "now playing" view up in those cases
 * hijacks whatever the user was actually doing.
 */
function playFromQueue(contentId) {
  if (contentId == null) return;
  openPlayer(contentId, {
    expanded: !document.getElementById("player-overlay").hidden,
    requireVisible: false,
  });
}

/**
 * Mirrors the queue into every control that depends on it: the overlay's
 * previous/next buttons, the mini bar's skip button, the shuffle toggle's
 * on-state, and the lock-screen transport. Driven by queue.js's
 * QUEUE_CHANGED event rather than called from each mutation site, so a new
 * way of changing the queue can't forget to update the UI.
 */
/**
 * The "Queue" panel inside the player overlay: open/close, and keep it
 * current while it's open.
 *
 * Only fetched while open. A queue is up to a thousand ids, and the panel is
 * closed the vast majority of the time — the same reasoning that keeps the
 * Downloads list out of refreshFragments()'s default sweep.
 */
/** Moves the "playing" marker without touching a single row's markup. */
function markCurrentQueueRow() {
  const playing = currentId();
  for (const row of document.querySelectorAll("#queue-panel-body .track-row")) {
    row.classList.toggle("is-current", Number(row.dataset.contentId) === playing);
  }
}

/**
 * Opens or closes the queue.
 *
 * Nothing scrolls and nothing is measured: the panel's height is the space
 * the card has spare, and the artwork gives that space up over the same
 * transition (see style.css's .queue-panel). The class on the overlay is
 * what stops the overlay scrolling while it's open, so the player can't be
 * pushed off the top of the screen by a flick through the list.
 *
 * Module-level rather than part of setupQueuePanel's closure because
 * collapsing or closing the player has to be able to shut the panel too.
 */
/**
 * Wide enough that the panel sits beside the player instead of opening
 * inside it. Must match the breakpoint in style.css — see the
 * `min-width: 900px` block by .player-main.
 */
const pinnedPanel = window.matchMedia("(min-width: 900px)");

function setQueueOpen(open) {
  const panel = document.getElementById("queue-panel");
  const toggle = document.getElementById("queue-toggle");
  const overlay = document.getElementById("player-overlay");
  if (!panel || !toggle || !overlay) return;
  // On a wide screen there is nothing to close: the panel has its own half of
  // the card and is open from the moment the player is. Forced here rather
  // than at each call site so that collapsing the player, closing it, or
  // dragging on it can all keep asking for a close and simply not get one —
  // and so that "is-open" still means "this panel is showing", which is what
  // the load and refresh paths below both test.
  if (pinnedPanel.matches) open = true;
  panel.classList.toggle("is-open", open);
  overlay.classList.toggle("is-queue-open", open);
  toggle.classList.toggle("is-on", open);
  toggle.setAttribute("aria-expanded", String(open));
}

// How far down the player has to be dragged before the queue closes: past
// the wobble in a tap, well short of a deliberate pull.
const QUEUE_DRAG_CLOSE_PX = 48;

function setupQueuePanel() {
  const toggle = document.getElementById("queue-toggle");
  const panel = document.getElementById("queue-panel");
  if (!toggle || !panel) return;

  // The order the rows on screen were drawn from, so a QUEUE_CHANGED can tell
  // "the pointer moved" from "the list is different".
  let rendered = [];

  // The full order, not just what's ahead: the panel is a fixed list and the
  // marker moves down it (see queue.js's queueOrder).
  const load = async () => {
    const order = queueOrder();
    const ok = await refreshQueuePanel(order);
    if (ok) rendered = order;
    markCurrentQueueRow();
    return ok;
  };

  toggle.addEventListener("click", () => {
    const opening = !panel.classList.contains("is-open");
    // The class goes on first and the rows land whenever they land. The panel
    // opens to a share of the card's height rather than to the height of its
    // contents, so the animation has nothing to wait for — and waiting is
    // what used to make the button read as doing nothing for a moment and
    // then jumping.
    setQueueOpen(opening);
    if (opening) load();
  });

  // Pull the panel's own top edge down and it closes — the gesture that
  // dismisses a sheet everywhere else, taken from the same place everywhere
  // else takes it.
  //
  // It used to be the whole card, minus the queue and the controls, which in
  // practice meant the artwork and the title: the two things furthest from
  // the panel being dismissed. Reaching over a track list to drag a cover
  // downwards is not a gesture anyone arrives at on their own. The tab strip
  // is the sheet's top edge and sits directly above what moves.
  //
  // Guarded like the toggle and panel above. This used to be dereferenced
  // bare, so a template change that dropped the element didn't degrade the
  // player — it threw here at boot and took every later setup call in
  // pages/index.js down with it, leaving a blank app rather than a broken
  // drag gesture.
  const handle = panel.querySelector(".panel-tabs");
  if (!handle) return;
  let dragFrom = null;
  // When the drag last closed the panel. A drag that started on a tab must
  // not also count as a tap on it, and the click for that tap arrives after
  // the panel has already gone.
  //
  // A timestamp rather than a "swallow the next click" flag, which is what
  // this was first: a touch drag doesn't always produce a click at all, and
  // the flag then sat armed until the *next* tap — a real one — was eaten
  // instead. Caught in a browser test, where the tab tapped after a
  // pull-to-close silently didn't switch. A window can only ever be wrong
  // about a click within a few hundred milliseconds of a real drag.
  let closedByDragAt = 0;
  const CLICK_AFTER_DRAG_MS = 400;

  handle.addEventListener("pointerdown", (event) => {
    // Nothing to dismiss where the panel isn't a drawer.
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
  // Capture, so it runs on the strip before the click reaches the tab button
  // itself — home/lyrics.js's handlers are bound to those buttons, and
  // stopping it here is what keeps a drag from also switching the tab it
  // happened to start on.
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

  // Everything that changes the queue lands here. Two different jobs: when
  // only the pointer moved — a track ended, or one of these very rows was
  // picked — the list on screen is still correct and re-fetching it would
  // rebuild it under the user, so only the marker moves. A different order
  // (shuffle toggled, a new queue loaded) genuinely needs new rows.
  document.addEventListener(QUEUE_CHANGED, () => {
    if (!panel.classList.contains("is-open")) return;
    const order = queueOrder();
    if (order.length === rendered.length && order.every((id, i) => id === rendered[i])) {
      markCurrentQueueRow();
      return;
    }
    load();
  });

  // Pinned open from the start on a wide screen, and across a resize that
  // crosses the breakpoint either way. Loading here rather than when the
  // player opens costs one request for an empty queue: from then on the
  // QUEUE_CHANGED handler above keeps the panel current, because the panel
  // counts as open the whole time.
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
  // Hidden rather than disabled on the bar: the overlay's row keeps its shape
  // so the play button stays put, but the bar has no shape to keep and a dead
  // control there is just clutter.
  document.getElementById("mini-player-next").hidden = !hasNext;
  document.getElementById("mini-player-prev").hidden = !hasPrevious;

  const shuffleBtn = document.getElementById("player-shuffle");
  shuffleBtn.classList.toggle("is-on", isShuffled());
  shuffleBtn.setAttribute("aria-pressed", String(isShuffled()));

  // One button, three states. The label has to say which one is on: "Repeat"
  // on its own leaves a screen reader with no way to tell them apart, and
  // the difference between the two icons is a single numeral.
  const repeat = repeatMode();
  const repeatBtn = document.getElementById("player-repeat");
  repeatBtn.dataset.repeat = repeat;
  repeatBtn.classList.toggle("is-on", repeat !== "off");
  repeatBtn.setAttribute(
    "aria-label",
    { off: "Repeat off", all: "Repeat queue", one: "Repeat this song" }[repeat]
  );
  // toggleAttribute, not `.hidden =`. These are <svg> elements, and
  // SVGElement has no `hidden` IDL property — the assignment quietly created
  // a plain JS property and never touched the attribute, so CSS's
  // `svg[hidden]` never matched and the icon never changed. Both repeat
  // states therefore drew the same icon: pressing twice looked like the
  // button had stuck on. (player.js's showIcon carries the same note; this
  // is the one place that forgot it.)
  document.getElementById("icon-repeat").toggleAttribute("hidden", repeat === "one");
  document.getElementById("icon-repeat-one").toggleAttribute("hidden", repeat !== "one");

  if (!("mediaSession" in navigator)) return;
  // Nulled rather than left registered when there's nowhere to skip to: the
  // handler's presence is what decides whether the OS draws the button at
  // all, so a no-op handler would put a dead control on the lock screen.
  // Wrapped because a browser that doesn't implement these actions throws
  // rather than ignoring them, which would take the rest of this sync with it.
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

  // Dismissing the player dismisses what it was working through. Leaving the
  // queue loaded would mean the next single track opened from a Home shelf
  // silently inherited a list the user has already closed.
  clearQueue();

  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  }
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

  // The progress line along the mini-bar's top edge. It used to be a passive
  // strip; on desktop this bar is the player for most of a listening session,
  // so it seeks — by click anywhere along it, and by arrow key, since it is
  // exposed as a slider.
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

  // A track running out is the whole point of having a queue; with none
  // loaded nextId() is null and playback simply stops, as it always did.
  onPlayerEvent("ended", () => {
    const root = document.getElementById("player-root");
    const finished = root.dataset.contentId;
    // A track switch no longer stops the outgoing track (see openPlayer), so
    // one can now run out while a *different* one is still downloading: the
    // element is still on the old resource, but the DOM and the queue pointer
    // already describe the incoming one. Advancing on that would step
    // straight over the track that's on its way in.
    if (finished && loadedTrackId() !== finished) {
      reportPlayback("outgoing-ended", { contentId: finished });
      return;
    }
    // Repeat "one" only means anything here — pressing Next still means the
    // next track. Rewinding and replaying rather than reopening the track
    // keeps the already-loaded resource, so there's no "Preparing audio…"
    // between loops.
    if (repeatMode() === "one") {
      const audio = activeAudio();
      audio.currentTime = 0;
      reportPlayback("track-ended", { contentId: finished, next: finished, repeat: "one" });
      audio.play().catch(() => {});
      return;
    }
    const next = nextId();
    // The first breadcrumb of a handoff, and the one that makes the rest
    // legible: everything after it in the log either happened in this same
    // event or didn't happen at all. See player.js's reportPlayback.
    //
    // `buffered` says whether the handoff has the next track's bytes in
    // memory, i.e. whether the src swap is against a blob or the network.
    // `prepared` alone could not: it only covers the metadata, and during
    // the 2026-08-23 stall investigation the difference had to be inferred
    // from the *absence* of a /stream request in the server's access log.
    reportPlayback("track-ended", {
      contentId: finished,
      next,
      prepared: upcomingTrack?.id ?? null,
      buffered: Boolean(upcomingTrack?.objectUrl),
    });
    playFromQueue(next);
  });

  // Downloads are triggered by playing something, so without this every
  // track change in a queue costs the same 2-4s "Preparing audio…" wait as
  // the first one — on a "Play all" that's a gap between every pair of
  // tracks. Fetching one ahead covers it, since a track that's already on
  // disk starts instantly.
  //
  // This used to hold off until the current track had played for 8 seconds,
  // so that skipping quickly through a queue didn't kick off a download per
  // track passed over. The cost of that was paid by the listener rather than
  // the skipper: press Next inside those 8 seconds — which is most of the
  // time anyone presses it at all — and the prefetch had not run, so the
  // press paid for the metadata round trip, the live song-version search
  // behind it, and the whole download.
  //
  // So it goes out with the track now. `timeupdate` rather than the open
  // itself because it is also the retry: "Play all" builds the queue *after*
  // opening the first track, so peekNextId() is still null at that point,
  // and this fires again a quarter of a second later when it isn't. A track
  // genuinely skipped past before it ever plays a frame still prefetches
  // nothing, since no timeupdate ever fires for it.
  //
  // What it does cost: skipping through tracks that each play for a moment
  // now starts a download for each one's successor. Server-side that is a
  // no-op for anything already on disk (see routers/content.py's
  // start_download), but a fresh one is a real yt-dlp run against YouTube.
  let prefetchedFor = null;
  onPlayerEvent("timeupdate", () => {
    const playing = document.getElementById("player-root").dataset.contentId;
    if (!playing || prefetchedFor === playing) return;
    const upcoming = peekNextId();
    if (upcoming == null) return;
    // Marked only once the prefetch is actually going out. Setting it before
    // the queue had been consulted made the guard permanent for that track: a
    // queue that was momentarily empty on this tick never got a second
    // chance, however long it went on playing — which is exactly the state
    // "Play all" is in on its first tick, since it builds the queue after
    // opening the track.
    prefetchedFor = playing;
    cacheUpcoming(upcoming);
  });

  document.getElementById("prev-track").addEventListener("click", () => playFromQueue(previousId()));
  document.getElementById("next-track").addEventListener("click", () => playFromQueue(nextId()));
  document.getElementById("mini-player-next").addEventListener("click", () => playFromQueue(nextId()));
  document.getElementById("mini-player-prev").addEventListener("click", () => playFromQueue(previousId()));
  document.getElementById("player-shuffle").addEventListener("click", () => toggleShuffle());
  // Both announce a QUEUE_CHANGED, which is what repaints the buttons — no
  // handler here touches its own control's appearance.
  document.getElementById("player-repeat").addEventListener("click", () => cycleRepeat());

  setupQueuePanel();

  document.addEventListener(QUEUE_CHANGED, syncQueueControls);
  syncQueueControls();

  // Re-asserted the moment audio is genuinely coming out, for the same reason
  // applyNowPlayingMetadata is (see player.js): iOS only reliably accepts a
  // Now Playing update while the page holds the audio session, and every
  // QUEUE_CHANGED that matters to the *first* track of a queue fires before
  // there is one. "Play all" builds the queue and then opens the player
  // (home/detail.js), so the only setActionHandler("nexttrack") call track one
  // ever gets lands in the silent gap before playback — iOS drops it, decides
  // the page has no track controls, and falls back to drawing the ±15s seek
  // pair instead. From track two on, every queue change happens mid-playback
  // and is taken, which is why the buttons appear for the rest of the session
  // and only the first track is ever wrong.
  onPlayerEvent("playing", syncQueueControls);

  // Collapsed rather than closed: leaving the overlay up would put the
  // artist's page behind it with no sign anything had happened, and closing
  // it outright stops the music. The mini bar is one tap back.
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

  // Home's shelves only — Library's grid links to channels/playlists, not
  // tracks (home/library.js handles those, via home/detail.js), and
  // Explore's results and the detail panel's track rows route through
  // playSearchedVideo and home/detail.js respectively instead.
  const homeTab = document.getElementById("tab-home");
  if (!homeTab) return;

  homeTab.addEventListener("click", (event) => {
    // Let ctrl/cmd/shift-click and middle-click behave natively (open a new
    // tab on this same #player/{id} hash, which handleInitialRoute resolves
    // on boot) instead of hijacking them.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    const link = event.target.closest("a");
    if (!link) return;
    const card = event.target.closest(".card");
    if (!card) return;

    event.preventDefault();
    const contentId = card.dataset.contentId;
    openPlayer(contentId);

    // Playing one card queues up the rest of its shelf, so "next track" means
    // something from Home too. Every content shelf is one of the pinned
    // playlists (see _home_shelves.html's data-queue-kind), which is what
    // makes this a queue the server can already produce.
    //
    // Deliberately not awaited, and deliberately after openPlayer: the queue
    // costs a round trip, and holding playback for it would put a network
    // call between the tap and play(), which is exactly what iOS refuses to
    // treat as a user gesture. The queue arriving late is invisible — it only
    // enables previous/next, and setQueue puts the pointer on the track that
    // is by then already playing.
    const kind = card.closest("[data-queue-kind]")?.dataset.queueKind;
    if (kind) loadQueue({ kind }, { startId: contentId });
  });
}

// #player-root has no server-rendered content id — the overlay starts every
// fresh page load closed and empty — so it has to be explicitly reopened
// before prepareAudio's resume logic has anything to attach to.
export function resumeOverlayIfNeeded() {
  const root = document.getElementById("player-root");
  if (!root || root.dataset.contentId) return;
  const saved = readResumeState();
  // wasExpanded !== false (not just "if true") so an older resume record
  // written before this flag existed still defaults to expanded.
  if (saved?.contentId) openPlayer(saved.contentId, { expanded: saved.wasExpanded !== false });
}
