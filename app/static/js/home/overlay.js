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

import { applyAmbientTint } from "./ambient.js";
import { api, formatDuration, showToast } from "../core.js";
import { refreshFragments, refreshQueuePanel } from "../fragments.js";
import {
  offlinePlaybackOn,
  openCoverUrl,
  openTrackUrl,
  readTrackMeta,
  storeTrack,
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

// Arrow-key step on the mini bar's progress slider. Matches player.js's
// SKIP_SECONDS so scrubbing feels the same wherever the focus happens to be.
const SEEK_STEP_SECONDS = 15;

// How long the prefetch will follow its own download before giving up, so the
// handoff knows whether the next track is actually playable before it gets
// there (see cacheUpcoming). Bounded well inside the length of a track — a
// download that hasn't landed by then won't be helped by asking again, and
// the handoff falls back to preparing it the ordinary way.
//
// The *cadence* inside that budget is player.js's nextPollDelay rather than a
// grid of this module's own. It used to be a flat 1.5s, which is precisely the
// arrangement player.js measured and abandoned — and the prefetch path simply
// never got the fix. On a real device on 2026-08-27 it cost 0.95s, 0.99s and
// 1.69s of dead air on three consecutive tracks: the file was on the server's
// disk and the client had not asked yet. One of those three tracks then missed
// its handoff by 1.4s.
const UPCOMING_POLL_BUDGET_MS = 30000;

// How close to a track's end the background early handoff fires (see the
// timeupdate handler in setupPlayerOverlay). Wide enough that the roughly
// once-a-second timeupdate cadence of a backgrounded iOS page still gets a
// tick inside the window; the cut it makes is at most this much of a track
// whose successor was about to cut it off anyway.
const EARLY_HANDOFF_SECONDS = 1.2;

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

// The prefetch's own transfer, so it can be called off.
//
// A superseded prefetch is not merely useless, and that is the point of
// holding this. On the commonest miss — the bytes had not landed by the time
// the handoff came for them — the file it is still pulling down is the exact
// file the <audio> element has just started fetching for itself, so it spends
// the whole of the buffer-up competing with the playback it existed to
// smooth, for a Blob that is thrown away on arrival. Measured on 2026-08-27:
// five range requests from the element over six seconds with two full copies
// of the same track in flight beside them, and `playback-stalled` at
// readyState 0.
let upcomingAbort = null;

function abortUpcomingFetch() {
  upcomingAbort?.abort();
  upcomingAbort = null;
}

// Which track's successor has already been sent for, so the several triggers
// below collapse into one prefetch per track played.
let prefetchedFor = null;

/**
 * Sends for whatever the queue says comes next.
 *
 * Fired from three places, and idempotent so that costs nothing: the track
 * opening, the queue changing, and every timeupdate after that. Each covers a
 * case the others miss — an open with no queue yet ("Play all" builds it
 * after opening the first track), a queue that arrives while a track is
 * already playing, and a track whose queue only becomes non-empty later.
 *
 * The open is the one that matters, and it was missing. This used to hang off
 * timeupdate alone, which sounds equivalent and is not: **a stalled element
 * emits no timeupdate**. So the moment one track failed to start promptly,
 * the next one's preparation did not begin either — measured on a device on
 * 2026-08-27, twice: a normal track sent for its successor 1.0-1.9s in, and a
 * track that stalled sent for its successor 7.4s and 7.9s in. Both of those
 * successors then stalled in turn. Firing on the open is what breaks that
 * chain, and it is worth most in exactly the conditions that create it.
 *
 * Marked only once the prefetch is actually going out. Setting it before the
 * queue had been consulted made the guard permanent for that track: a queue
 * that was momentarily empty on this tick would never get a second chance,
 * however long the track went on playing.
 */
function prefetchUpcoming() {
  const playing = document.getElementById("player-root")?.dataset.contentId;
  if (!playing || prefetchedFor === playing) return;
  const upcoming = peekNextId();
  if (upcoming == null) return;
  prefetchedFor = playing;
  cacheUpcoming(upcoming);
}

// The blob: URL currently in the player's <img>, when the track came off
// the device. Held so the next open can revoke it — nothing else has a
// reference once the src is reassigned.
let playerCoverUrl = null;

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

// `coverSrc` is whatever the full player settled on, passed in rather than
// re-derived: for a track playing off the device that is a blob: URL for the
// saved cover, and letting this fall back to data.thumbnail_url would leave
// the mini bar reaching for /image-proxy — the one request an offline track
// is meant not to make. Both surfaces then show the same artwork, which is
// the only sane outcome given they are the same track.
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
  } else {
    // `prepared` is the whole point of reporting this: it separates "the
    // prefetch never ran for this track" from "it ran for a different one".
    reportPlayback("handoff-missed", { contentId, prepared: upcomingTrack?.id ?? null });
    if (upcomingTrack?.objectUrl) {
      // Bytes pulled down for a track this open isn't going to. Nothing will
      // ever read them, and an object URL pins its Blob until revoked.
      URL.revokeObjectURL(upcomingTrack.objectUrl);
    }
  }
  upcomingTrack = null;
  // Whatever it had not finished pulling down belongs to nobody now: this
  // open has already read objectUrl and taken whatever was there, so the rest
  // of that transfer can only ever be discarded. Stopping it is what keeps it
  // out of the way of the element, which on a miss is fetching the very same
  // track — see abortUpcomingFetch.
  abortUpcomingFetch();

  // Nothing was prepared for this open — but the track may be one the user
  // keeps on the device, in which case its bytes are already here and the
  // /stream request never needs to happen at all. Only consulted on the miss:
  // a prefetch hit already holds the bytes, so a lookup there would add an
  // await to the one path where the handoff has to be instant (an
  // auto-advance while iOS has the app frozen — see cacheUpcomingAudio).
  //
  // On the miss it is the opposite: this replaces a whole-track network fetch
  // with a local read, so a saved track auto-advances with no network at all.
  let playingFromDevice = false;
  if (!prefetchedAudio) {
    const savedUrl = await openTrackUrl(contentId);
    if (savedUrl) {
      prefetchedAudio = savedUrl;
      playingFromDevice = true;
      reportPlayback("handoff-device", { contentId });
    }
  }

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
    // The server is the only place a title normally comes from, and offline
    // it is the one thing that cannot be reached — so a saved track would
    // fail to open with its own audio sitting in the page, which is the exact
    // situation the whole feature exists for. What was stored alongside the
    // bytes stands in (see offline.js's readTrackMeta).
    //
    // Only when the request never arrived. A 404 or a 409 is the server
    // answering, and answering that this track is gone or not ready — that is
    // a real answer and it wins over a local copy's memory of it.
    if (!res.ok && res.status === 0 && playingFromDevice) {
      const meta = await readTrackMeta(contentId);
      if (meta) {
        data = {
          id: Number(contentId),
          title: meta.title,
          channel_title: meta.artist || "",
          // Neither is knowable without the server. `offline` is what the
          // controls below read to disable rather than mislead: an artist
          // page cannot be opened, and a favorite cannot be recorded, so a
          // heart rendered "off" would be a claim this has no way to make.
          artist_page_id: null,
          is_favorite: false,
          offline: true,
          // The cover comes off the device below, so this stays null rather
          // than pointing at /image-proxy — a request, on the one path that
          // must make none.
          thumbnail_url: null,
          duration_seconds: meta.duration ?? null,
          status: "ready",
          is_unavailable: false,
        };
        reportPlayback("opened-offline", { contentId });
      }
    }
    if (!data && !res.ok) {
      // Two different failures, and offline the generic one is actively
      // misleading: nothing is wrong with the track, it simply isn't one of
      // the ones kept on this device. Saying so is also the only place the
      // app can teach what the phone button in Downloads is for.
      showToast(
        res.status === 0
          ? "You're offline — this song isn't saved to this device"
          : "Could not load this track"
      );
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
      // This is a local lookup miss (404) or an offline device with nothing
      // saved — never a YouTube request, so it's the same shape as the
      // is_unavailable case below and gets that cap rather than the tighter
      // one reserved for actual download failures. Without this, a track that
      // fails here (as opposed to failing inside prepareAudio further down)
      // just left playback dead — the exact "doesn't auto-advance" gap that
      // this mirrors the fix for.
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
    // Not when the offline fallback above already built it: res.data is null
    // in that case, and songVersionOf is a live YouTube lookup — the one call
    // guaranteed to fail on the path that got here precisely because nothing
    // can reach the network.
    if (!data) {
      data = res.data;
      data = await songVersionOf(data);
    }
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
  // The cover saved alongside the audio, for a track being played from the
  // device. data.thumbnail_url points at /image-proxy, which is a request —
  // so on the network this track no longer needs, the art would be the one
  // thing still reaching for it, and would come back blank.
  const savedCover = playingFromDevice ? await openCoverUrl(contentId) : null;
  // The previous track's, now that nothing is showing it. An object URL pins
  // its Blob until revoked, and these are whole images.
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
  // What the OS gets, which is deliberately not what the element above got:
  // the page needs one image at the size it draws, and Now Playing needs the
  // same picture at several declared sizes it can choose between (see
  // player.js's setNowPlayingArtwork and images.track_artwork). It is also
  // the only cover the OS can use for a track playing off the device — the
  // element is showing a blob: URL in that case, which the OS cannot fetch.
  setNowPlayingArtwork(data.artwork);
  // Purely the backdrop colour behind this card — it reads the image that was
  // just set and writes two custom properties. Nothing here touches the audio
  // element, the queue, or anything else on the playback path.
  applyAmbientTint();
  document.getElementById("duration-time").textContent = data.duration_seconds
    ? formatDuration(data.duration_seconds)
    : "0:00";

  const favBtn = document.getElementById("favorite-btn");
  // Favoriting is a write to the server, so offline it cannot happen — and a
  // live-looking heart that silently drops the press is worse than one that
  // says it is unavailable. Same treatment the artist link above gets for the
  // same reason.
  favBtn.disabled = data.offline === true;
  favBtn.dataset.contentId = data.id;
  favBtn.dataset.favorite = String(data.is_favorite);
  favBtn.classList.toggle("is-on", data.is_favorite);
  favBtn.setAttribute("aria-pressed", String(data.is_favorite));
  favBtn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

  root.dataset.contentId = String(data.id);
  // What the server says about its own copy stops mattering once the bytes
  // are on the device. Both of these would otherwise refuse a track that is
  // sitting right here: a library cleared from the Downloads modal leaves the
  // row "not_downloaded", which prepareAudio answers by starting a fresh
  // download, and an `is_unavailable` row is skipped outright — a state a
  // saved track can genuinely reach, since YouTube pulling a video has no
  // bearing on a copy that was taken before it did.
  root.dataset.status = playingFromDevice ? "ready" : data.status;
  root.dataset.unavailable = String(!playingFromDevice && data.is_unavailable === true);
  root.dataset.stream = `/content/${data.id}/stream`;

  syncMiniPlayerInfo(data, coverSrc);

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

  // Before prepareAudio, not after: this track may be about to spend seconds
  // downloading, and the whole point is that the next one's preparation runs
  // alongside that rather than behind it.
  prefetchUpcoming();

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
 *
 * Only the cold open still calls this — the prefetch dropped it when the
 * download started doing the swap itself. That path can't: it renders the
 * title and the cover from this row *before* anything asks for a download, so
 * taking the swap off the download's answer would leave the music video's name
 * and its 16:9 still on screen under a song that is already playing. It costs
 * a round trip once, on the one open where the listener is watching a spinner
 * anyway, rather than on every track of a queue.
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
  // The queue named a different successor than the one still coming down.
  abortUpcomingFetch();

  // One request where there used to be three. This used to fetch the row,
  // POST the song swap, and only then POST the download — in that order, and
  // the ordering was this module's to get right, because the download fetches
  // whatever video_id the row currently names. The server does the swap on its
  // way into the download now (see routers/content.py's start_download), so
  // the ordering is guaranteed rather than arranged, and the answer carries
  // the row it ended up with.
  const download = await api(`/content/${id}/download`, { method: "POST" });

  let data = null;
  if (download.data?.content) {
    data = { ...download.data.content };
    data.status = download.data.status;
    data.is_unavailable = download.data.is_unavailable === true;
  } else {
    // No row in that answer, which is a 409 nearly every time: something else
    // already has this one downloading — another tab, or this one having sent
    // for the same successor twice across a queue change. The track is
    // perfectly fine, it just isn't this response's to describe, so ask for it
    // the old way and carry on into the poll below. Left as the slow path
    // deliberately: it is the uncommon one, and paying a round trip for it is
    // what keeps the common one down to a single request.
    const meta = await api(`/content/${id}`);
    if (!meta.ok) return;
    data = { ...meta.data };
  }

  // Published here, where it used to go up between the swap and the download.
  // The window a Next press can land in and find nothing is much the same
  // either way — the live song search dominates both arrangements — and the
  // chain around it is two round trips shorter. What is not on offer is
  // publishing early off the *unswapped* row: the handoff renders straight
  // from this, so that would put the music video's title and its 16:9 still on
  // screen for a track that is about to play the song.
  upcomingTrack = { id, data, objectUrl: null };

  if (data.is_unavailable || data.status === "error") return;
  if (data.status === "ready") {
    await cacheUpcomingAudio(id);
    return;
  }

  const startedAt = Date.now();
  while (Date.now() - startedAt < UPCOMING_POLL_BUDGET_MS) {
    await new Promise((resolve) => setTimeout(resolve, nextPollDelay(Date.now() - startedAt)));
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
  // Hoisted so the finally can tell "my transfer is over" from "somebody
  // else's has already started": an abort rejects this call's fetch, and by
  // the time that rejection is handled the newer prefetch has usually
  // published its own controller. Clearing unconditionally would strand that
  // one, leaving the transfer that is actually running impossible to stop.
  let controller = null;
  try {
    // A track kept on the device is already downloaded, so the size cap below
    // has nothing to protect against here — it exists to stop a large
    // *transfer* being spent on a track nobody may listen to, and this is a
    // local read. Doing it at prefetch time rather than leaving it to
    // openPlayer's own device lookup is what keeps the handoff instant: the
    // frozen-iOS auto-advance has to find the bytes already in the page, not
    // go and await them at the moment the outgoing track runs out.
    const savedUrl = await openTrackUrl(id);
    if (savedUrl) {
      if (upcomingTrack?.id !== id) {
        URL.revokeObjectURL(savedUrl);
        return;
      }
      upcomingTrack.objectUrl = savedUrl;
      return;
    }

    // Plain /stream, no marker of its own: asking for it no longer records a
    // play (see routers/content.py's stream_content), which is precisely
    // what makes fetching a track early safe to do.
    controller = new AbortController();
    upcomingAbort = controller;
    // Read now rather than after the transfer: this is the metadata for the
    // track being fetched, and by the time the bytes land upcomingTrack may
    // have moved on to a different song entirely.
    const meta = upcomingTrack?.id === id ? upcomingTrack.data : null;
    const res = await fetch(`/content/${id}/stream`, { signal: controller.signal });
    if (!res.ok) return;
    // Declined before buffering when the server says how big it is, and
    // again afterwards for the case where it didn't.
    const declared = Number(res.headers.get("content-length"));
    if (Number.isFinite(declared) && declared > PREFETCH_MAX_BYTES) return;
    const blob = await res.blob();
    if (blob.size > PREFETCH_MAX_BYTES) return;
    objectUrl = URL.createObjectURL(blob);

    // The whole track is in the page now, and with offline playback on this
    // device is meant to end up holding it anyway. Keeping it here rather
    // than leaving it to the sync is what stops the same file being fetched
    // twice: the sync's own pass is scheduled by the fragment refresh that
    // playing this track triggers, so without this the second transfer landed
    // squarely on top of the first (see offline.js's storeTrack).
    //
    // Best effort and deliberately not awaited into the handoff's path: a
    // full device must not cost the listener the track that is already here.
    if (meta && offlinePlaybackOn()) {
      // Measured on a device on 2026-08-27, since web.dev's guidance is that
      // structured cloning runs on the main thread and scales with size:
      // 543-745ms for 2.8-3.7MB, on a write that is not awaited by anything.
      // Nowhere near the seconds that would make it worth moving off this
      // path, so it stays where the bytes already are.
      storeTrack(id, blob, {
        title: meta.title || "",
        artist: meta.channel_title || "",
        coverUrl: meta.thumbnail_url || null,
        duration: meta.duration_seconds ?? null,
      }).catch(() => {});
    }
  } catch (err) {
    return;
  } finally {
    // Aborted, failed, or finished — whichever, this call's transfer is over.
    if (upcomingAbort === controller) upcomingAbort = null;
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
    // A queue that has genuinely run out leaves nothing for the OS's Now
    // Playing surface to control: iOS freezes the page shortly after the
    // audio stops, so the card it would keep showing is dead weight — and a
    // dead card is exactly what lingers on the Dynamic Island after the app
    // is closed. Cleared here rather than left "paused" forever; replaying
    // the track in-app re-publishes everything on `playing`. The player
    // overlay itself stays exactly as it was.
    if (next == null) clearNowPlayingMetadata();
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
  // Then it went out on the first timeupdate instead, which was nearly right
  // and quietly kept the worst case: an element that never starts never ticks
  // (see prefetchUpcoming). The open is the trigger now, and these two are
  // what catch the opens that could not send for anything yet — "Play all"
  // builds its queue after opening the first track, so the open finds
  // peekNextId() null and the queue's own event is what starts it.
  //
  // What it does cost: skipping through tracks that each play for a moment
  // now starts a download for each one's successor. Server-side that is a
  // no-op for anything already on disk (see routers/content.py's
  // start_download), but a fresh one is a real yt-dlp run against YouTube.
  document.addEventListener(QUEUE_CHANGED, prefetchUpcoming);
  onPlayerEvent("timeupdate", prefetchUpcoming);

  // The early handoff: in the background, the next track starts *before*
  // this one ends, so the element never passes through `ended` off screen.
  //
  // `ended` is a cliff there. The moment nothing is rendering, a
  // backgrounded page is living on borrowed time — iOS froze one mid-handoff
  // within three seconds of `ended` on 2026-08-23 (18:14 in the breadcrumb
  // log) with the next track's bytes already in memory and play() already
  // called: everything after the silence was done right, and the track still
  // sat at readyState 1 until the screen woke 14 seconds later. The only
  // reliable side of the cliff is the near side, while audio is still
  // rendering and the page still provably holds the session.
  //
  // Only when hidden: in the foreground the page isn't at risk of being
  // frozen, the `ended` path below works, and cutting the tail off every
  // track would buy nothing. Only with the bytes in memory: a handoff that
  // still needs the network would trade the end of this track for a stall it
  // can't afford either — the `ended` path is no worse for that case. The
  // threshold accommodates background timeupdate cadence (roughly 1Hz on
  // iOS), so the actual cut is somewhere inside the last second-and-a-bit of
  // a track that is about to be cut off by its own successor anyway.
  //
  // Repeat-"one" takes the same protection as a rewind: looping by seeking
  // back before the end never reaches `ended` at all, where the handler
  // below rewinds only *after* it — on the far side of the cliff.
  let earlyHandoffFor = null;
  onPlayerEvent("timeupdate", () => {
    if (document.visibilityState === "visible") return;
    const audio = activeAudio();
    if (audio.paused) return;
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    if (audio.duration - audio.currentTime > EARLY_HANDOFF_SECONDS) return;

    const playing = document.getElementById("player-root").dataset.contentId;
    // No track, or a switch already underway (the DOM describes an incoming
    // track the element hasn't been handed yet) — nothing to hand off from.
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
