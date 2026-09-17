// Actions on music not yet in the library, shared by Explore and the detail panel.
// Must never import home/detail.js (cycle); ARTIST_FOLLOWED is fired instead.

export const ARTIST_FOLLOWED = "spotea:artist-followed";

import { api, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { openPlayer } from "./overlay.js";
import { queueSource, setQueue } from "./queue.js";

/** Adds the video as an unkept preview and opens its player. */
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
        // Read off the row, avoiding a YouTube Music request per click.
        artist_credit: dataset.artistCredit || null,
      },
      errorMessage: "Could not add this song",
    });
    if (!ok) return;
    // An explicit one-track queue, so the queue panel shows this track instead of going blank.
    setQueue({ kind: "single" }, [data.content_id]);
    openPlayer(data.content_id);
  } finally {
    if (button) button.disabled = false;
  }
}

function remoteRows() {
  return [...document.querySelectorAll("#detail-panel .track-row-remote")];
}

/**
 * Turns the listing's rows into library rows (previews, no YouTube cost). Resolves to
 * { items, data } with data.content_ids in items' order, or null (already reported).
 * Rows with no channel can't become library rows and are left out of items.
 */
export async function materializeRemoteRows(rows = remoteRows(), errorMessage = "Could not read this list") {
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
    return null;
  }
  const { ok, data } = await api("/explore/tracks/batch", { method: "POST", body: { items }, errorMessage });
  return ok ? { items, data } : null;
}

/** Ignores a double tap rather than sending a second batch that races the first. */
let listStartInFlight = false;

/** Plays a remote listing: all rows become previews in one request (no YouTube cost), then queue. */
export async function playRemoteList(source, { startVideoId = null, button = null } = {}) {
  const rows = remoteRows();
  if (!rows.length) return;
  if (listStartInFlight) return;

  listStartInFlight = true;
  if (button) button.disabled = true;
  try {
    const made = await materializeRemoteRows(rows, "Could not start this list");
    if (!made) return;
    const { items, data } = made;

    // Positional: add_video_batch answers in the order it was sent.
    const startIndex = startVideoId
      ? items.findIndex((item) => item.video_id === startVideoId)
      : -1;
    const clickedId = startIndex === -1 ? null : data.content_ids[startIndex];

    // Another row of the list already playing must not rebuild the queue (it would
    // reshuffle); "Play all" always rebuilds, which restarts the list.
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

/** Follows an artist. `added` is also true for a 409, meaning already followed. */
export async function followArtist(channelUrl, button, { announce = true } = {}) {
  const originalLabel = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Adding…";
  }

  const { ok, status, data } = await api("/artists", {
    method: "POST",
    body: { channel_url: channelUrl },
  });

  if (ok) {
    if (data?.artist?.id == null) {
      window.location.reload();
      return { added: false, status };
    }
    if (button) button.textContent = "Following";
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
