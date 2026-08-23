// Acting on music the library doesn't have yet: an Explore search result, a
// recommended song, a track of a YouTube Music playlist, or a whole artist.
//
// Split out of home/explore.js because the detail panel needs the same three
// actions (its remote rows are Explore results rendered somewhere else), and
// this module can be imported by both without a cycle: it never imports
// home/detail.js. Where it would need to — opening the artist's page once a
// follow finishes — it fires ARTIST_FOLLOWED and lets home/detail.js react,
// the same one-way arrangement home/queue.js uses for QUEUE_CHANGED.

export const ARTIST_FOLLOWED = "spotea:artist-followed";

import { api, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { openPlayer } from "./overlay.js";
import { queueSource, setQueue } from "./queue.js";

/**
 * Explore's "listen" action: adds the video (always as an unkept preview —
 * see routers/explore.py's add_single_video) and jumps straight to its
 * player. If this video already has a Content row, add_single_video hands
 * back that row's id instead of erroring, so this just replays whatever was
 * already downloaded. No backfill-overlay wait here — unlike a follow
 * this is a single insert, so it should feel instant.
 */
export async function playRemoteVideo(dataset, button) {
  if (button) button.disabled = true;
  try {
    const { ok, data } = await api("/explore/tracks", {
      method: "POST",
      body: {
        video_id: dataset.videoId,
        title: dataset.title,
        channel_id: dataset.channelId,
        thumbnail_url: dataset.thumbnailUrl || null,
        duration_seconds: dataset.durationSeconds ? Number(dataset.durationSeconds) : null,
        channel_title: dataset.channelTitle || null,
        // The track's own credit line. Read back off the row rather than
        // resolved server-side for the same reason channel_id is: it came
        // down with the listing that rendered this row, and asking YouTube
        // Music again would cost a request per click.
        artist_credit: dataset.artistCredit || null,
      },
      errorMessage: "Could not add this song",
    });
    if (!ok) return;
    // A queue of exactly this one track, rather than none.
    //
    // Nothing here came out of a list — a single, an Explore result, one of
    // an artist's videos — so there is no "rest of it" to line up, and
    // pressing play on one of these deliberately does not inherit whatever
    // queue happened to be loaded before. Setting it explicitly rather than
    // letting noteCurrent clear it keeps the queue panel showing the track
    // that is actually playing instead of going blank, which matters most
    // on desktop where that panel is always open.
    setQueue({ kind: "single" }, [data.content_id]);
    openPlayer(data.content_id);
  } finally {
    if (button) button.disabled = false;
  }
}

/** The remote track rows currently rendered in the detail panel, in order. */
function remoteRows() {
  return [...document.querySelectorAll("#detail-panel .track-row-remote")];
}

/**
 * Plays a whole remote playlist/channel listing: turns every row into a
 * preview Content row in one request, hands the resulting ids to the queue,
 * and starts on `startVideoId` (a clicked row) or wherever the queue's order
 * begins (Play all, which under shuffle is a random track).
 *
 * The batch costs no YouTube requests — every field those rows need came back
 * with the listing itself (see routers/explore.py's add_video_batch) — so
 * materializing the list up front is cheap, and everything downstream (the
 * one-track-ahead download prefetch, auto-advance, prev/next, shuffle) is the
 * ordinary queue with nothing special about it.
 *
 * The rows are read from the DOM rather than re-fetched: a remote listing has
 * no second page and no /content/queue/... endpoint, so what's on screen is
 * by definition the whole list.
 */
/**
 * Whether a "start this list" request is already on its way.
 *
 * "Play all" passes its button and gets disabled for the duration; clicking a
 * row passes none, so nothing stopped a double tap sending two of these at
 * once. Both then read the same "none of these exist yet" answer off the
 * database and both insert — a UNIQUE violation for whichever commits second,
 * which surfaced as a 500 and, because a 500's body isn't JSON, as api()'s
 * fallback toast: "Could not start this list". The server retries past it now
 * (see routers/explore.py's add_video_batch); this is the half that stops the
 * pointless second request being made at all.
 *
 * Ignored rather than queued: the second tap is the same tap. Running it
 * after the first would rebuild the queue a moment after playback had already
 * started from it.
 */
let listStartInFlight = false;

export async function playRemoteList(source, { startVideoId = null, button = null } = {}) {
  const rows = remoteRows();
  if (!rows.length) return;
  if (listStartInFlight) return;

  listStartInFlight = true;
  if (button) button.disabled = true;
  try {
    const items = rows
      .filter((row) => row.dataset.channelId)
      .map((row) => ({
        video_id: row.dataset.videoId,
        channel_id: row.dataset.channelId,
        title: row.dataset.title,
        thumbnail_url: row.dataset.thumbnailUrl || null,
        duration_seconds: row.dataset.durationSeconds ? Number(row.dataset.durationSeconds) : null,
        channel_title: row.dataset.channelTitle || null,
        artist_credit: row.dataset.artistCredit || null,
      }));
    if (!items.length) {
      showToast("Nothing to play here");
      return;
    }

    const { ok, data } = await api("/explore/tracks/batch", {
      method: "POST",
      body: { items },
      errorMessage: "Could not start this list",
    });
    if (!ok) return;

    // Positional: add_video_batch answers in exactly the order it was sent,
    // which is what lets a clicked row be found without matching on ids.
    const startIndex = startVideoId
      ? items.findIndex((item) => item.video_id === startVideoId)
      : -1;
    const clickedId = startIndex === -1 ? null : data.content_ids[startIndex];

    // Clicking a second row of a list that's already playing shouldn't
    // rebuild the queue — under shuffle that would reshuffle the rest around
    // the new track. Same reasoning as home/detail.js's alreadyQueued check
    // for local rows. "Play all" (no clicked row) always rebuilds, which is
    // what makes pressing it again start the list over.
    const queued = queueSource();
    if (clickedId != null && queued && queued.kind === source.kind && String(queued.id) === String(source.id)) {
      openPlayer(clickedId);
      return;
    }

    const startId = setQueue(source, data.content_ids, { startId: clickedId });
    if (startId != null) openPlayer(startId);
  } finally {
    listStartInFlight = false;
    if (button) button.disabled = false;
  }
}

// --- Following a channel ---------------------------------------------------

/**
 * Follows an artist and reports back as { added, status }: `added` is whether
 * they are now followed — true for a fresh follow, and also for a 409, which
 * means some earlier action already added them.
 *
 * Nothing is waited on. POST /artists answers as soon as the row exists, and
 * the first sync behind it is a background task the Library card reports on
 * itself (see page_context.library_context and home/library.js). The
 * full-screen overlay this used to put up existed for a history scan that
 * could run for minutes; there is no such scan any more.
 */
export async function followArtist(channelUrl, button, { announce = true } = {}) {
  const originalLabel = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Adding…";
  }

  const { ok, status, data } = await api("/artists", {
    method: "POST",
    // Only the channel: which artist it is, and whether it is one at all, is
    // worked out server-side (see services/artist_follow.py).
    body: { channel_url: channelUrl },
  });

  if (ok) {
    if (data?.artist?.id == null) {
      window.location.reload();
      return { added: false, status };
    }
    // "Following" — the same word the artist page's own button uses once you
    // follow (see _detail_hero.html), so the state reads the same wherever
    // you happen to have followed from.
    if (button) button.textContent = "Following";
    // The grid needs re-rendering to pick up the new card and its
    // "still fetching" state.
    refreshFragments();
    if (announce) {
      document.dispatchEvent(
        new CustomEvent(ARTIST_FOLLOWED, {
          detail: {
            artistId: data.artist.id,
            title: data.artist.name || channelUrl,
            browseId: data.artist.browse_id || null,
          },
        })
      );
    }
    return { added: true, status };
  }

  if (status === 409) {
    if (button) button.textContent = "Already added";
    return { added: true, status };
  }
  if (button) {
    button.disabled = false;
    button.textContent = originalLabel;
  }
  if (status !== 0) showToast(data?.detail || "Could not follow this artist");
  return { added: false, status };
}
