// Lists the user made themselves: creating one, putting the playing track in
// one, taking a track back out, and deleting one.
//
// The lists *render* through the machinery that was already here — Library's
// grid draws its tiles from library_context, and opening one swaps in
// /partials/detail/user-playlist/{id}, which is the same _detail_panel.html
// every other track list uses. So there is no rendering here at all beyond
// the picker, whose contents (which lists already hold *this* track) is the
// one thing a page render cannot know in advance.

import { api, confirmDialog, escapeHtml, promptDialog, setupOverlay, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";

/**
 * "This playlist's contents changed" / "this playlist is gone", announced
 * rather than acted on.
 *
 * home/detail.js owns the panel — reopening it, and knowing whether Library
 * is behind it to go back to — and importing it here for that would be a
 * cycle, since it is what opens a playlist in the first place. Same one-way
 * arrangement OPEN_ARTIST and QUEUE_CHANGED use.
 */
export const PLAYLIST_CHANGED = "spotea:playlist-changed";
export const PLAYLIST_DELETED = "spotea:playlist-deleted";

// The track the picker is open for. Read back when a row in it is pressed,
// so the list doesn't have to carry the id on every row.
let pickerContentId = null;

/**
 * Asks for a name and creates it. Resolves to the new playlist, or null.
 *
 * `message` is what the dialog asks. The duplicate-name retry calls back in
 * with the reason as the question, rather than a toast that would appear
 * behind the modal that caused it.
 */
async function createPlaylist(message = "Give it a name you'll recognise later.") {
  const name = await promptDialog(message, {
    title: "New playlist",
    confirmLabel: "Create",
    placeholder: "Late night, Gym, Road trip…",
  });
  if (!name) return null;

  const { ok, status, data } = await api("/playlists", {
    method: "POST",
    body: { name },
  });
  if (ok) return data;

  // 409 is the one failure worth another go: the name is taken, and the user
  // is one edit away from a name that isn't.
  if (status === 409) return createPlaylist("You already have a playlist called that — try another name.");
  showToast("Could not create that playlist");
  return null;
}

/** Renders the picker's rows for whatever /playlists just returned. */
function renderPicker(playlists) {
  const list = document.getElementById("playlist-picker-list");
  if (!list) return;

  if (!playlists.length) {
    list.innerHTML =
      '<li class="playlist-picker-empty muted">No playlists yet — make one below.</li>';
    return;
  }

  // escapeHtml on the name: it is user-entered text going into both an
  // attribute and a text position (see core.js's note on why textContent
  // serialization is not enough for the attribute case).
  //
  // `isIn` is lifted out rather than written as a ternary inside the
  // attribute, so the interpolation is a bare identifier. That is what
  // test_escape_html_is_the_only_escaper_used_in_markup can see is safe — it
  // reads the expression, and a blanket-strict guard that has to reason about
  // ternaries is a guard with a hole in it. A boolean stringifies to exactly
  // the "true"/"false" aria-pressed wants.
  list.innerHTML = playlists
    .map((playlist) => {
      const isIn = playlist.contains === true;
      const plural = playlist.track_count === 1 ? "" : "s";
      return `
      <li>
        <button type="button" class="playlist-picker-row${isIn ? " is-in" : ""}"
                data-playlist-id="${playlist.id}"
                aria-pressed="${isIn}">
          <span class="playlist-picker-name">${escapeHtml(playlist.name)}</span>
          <span class="playlist-picker-count muted">${playlist.track_count} song${plural}</span>
        </button>
      </li>`;
    })
    .join("");
}

/** Fetches the list fresh, with `contains` filled in for the open track. */
async function loadPicker() {
  const { ok, data } = await api(`/playlists?content_id=${pickerContentId}`);
  if (!ok) {
    showToast("Could not load your playlists");
    return;
  }
  renderPicker(data || []);
}

async function openPicker() {
  const root = document.getElementById("player-root");
  const contentId = root?.dataset.contentId;
  if (!contentId) return;

  pickerContentId = contentId;
  const title = document.querySelector(".player-title")?.textContent || "";
  const label = document.getElementById("playlist-picker-track");
  if (label) label.textContent = title;

  document.getElementById("playlist-picker-overlay").hidden = false;
  // Opened first, filled after: the list is a round trip, and a picker that
  // waits for it reads as a press that did nothing.
  await loadPicker();
}

/** Puts the open track in a list, or takes it back out. */
async function togglePlaylistMembership(button) {
  const playlistId = button.dataset.playlistId;
  const isIn = button.getAttribute("aria-pressed") === "true";
  if (button.disabled) return;
  button.disabled = true;

  const { ok, data } = isIn
    ? await api(`/playlists/${playlistId}/tracks/${pickerContentId}`, {
        method: "DELETE",
        errorMessage: "Could not take it out of that playlist",
      })
    : await api(`/playlists/${playlistId}/tracks`, {
        method: "POST",
        body: { content_id: Number(pickerContentId) },
        errorMessage: "Could not add it to that playlist",
      });

  button.disabled = false;
  if (!ok) return;

  // "duplicate" means it was already there — the outcome the press asked for,
  // so it is a success with different wording rather than an error.
  showToast(isIn ? "Removed from playlist" : data?.status === "duplicate" ? "Already in that playlist" : "Added to playlist");
  // Re-read rather than toggling the button: the count on the row changed
  // too, and the server is the only thing that knows the new one.
  await loadPicker();
  // Library's tiles carry the counts as well.
  refreshFragments();
}

export function setupPlaylists() {
  setupOverlay("playlist-picker-overlay", "playlist-picker-close");

  document.getElementById("add-to-playlist-btn")?.addEventListener("click", openPicker);

  // Delegated from #playlist-picker-list's parent modal, because the list's
  // rows are rewritten on every change.
  document.getElementById("playlist-picker-overlay")?.addEventListener("click", (event) => {
    const row = event.target.closest(".playlist-picker-row");
    if (row) {
      togglePlaylistMembership(row);
      return;
    }
    if (event.target.closest("#playlist-picker-new")) {
      createPlaylist().then((playlist) => {
        if (!playlist) return;
        // Made from inside the picker, for a track the user is trying to
        // file — so putting the track in it is the whole point of having
        // pressed this, not a second step.
        api(`/playlists/${playlist.id}/tracks`, {
          method: "POST",
          body: { content_id: Number(pickerContentId) },
          errorMessage: "Made the playlist, but could not add this song",
        }).then(() => {
          loadPicker();
          refreshFragments();
        });
      });
    }
  });

  // #library-grid is the fragment's swap target rather than one of the nodes
  // swapped into it, so it survives every refresh and a listener here does
  // too (same reasoning as the Downloads modal's, see home/settings.js).
  document.getElementById("library-grid")?.addEventListener("click", (event) => {
    if (!event.target.closest("#new-playlist-btn")) return;
    createPlaylist().then((playlist) => {
      if (playlist) refreshFragments();
    });
  });

  // The detail panel's two playlist-only controls. A separate listener from
  // home/detail.js's rather than a branch inside it: neither target is
  // reachable through that one, since it routes row clicks via
  // ".track-row .track-link" and the remove button is that link's sibling.
  document.getElementById("detail-panel")?.addEventListener("click", (event) => {
    const removeBtn = event.target.closest(".track-remove");
    if (removeBtn) {
      removeFromPlaylist(removeBtn);
      return;
    }
    const deleteBtn = event.target.closest("#delete-playlist-btn");
    if (deleteBtn) deletePlaylist(deleteBtn.dataset.playlistId);
  });
}

async function removeFromPlaylist(button) {
  if (button.disabled) return;
  button.disabled = true;
  const { playlistId, contentId } = button.dataset;
  const { ok } = await api(`/playlists/${playlistId}/tracks/${contentId}`, {
    method: "DELETE",
    errorMessage: "Could not remove that song",
  });
  button.disabled = false;
  if (!ok) return;
  // The row is gone from the list, and so is one from the count in the hero
  // and on Library's tile — all of which the server renders, so the panel is
  // re-opened rather than having the row plucked out of the DOM here.
  document.dispatchEvent(new CustomEvent(PLAYLIST_CHANGED, { detail: { playlistId } }));
  refreshFragments();
}

async function deletePlaylist(playlistId) {
  const confirmed = await confirmDialog(
    "Delete this playlist? The songs stay in your library — only the list goes.",
    "Delete"
  );
  if (!confirmed) return;
  const { ok } = await api(`/playlists/${playlistId}`, {
    method: "DELETE",
    errorMessage: "Could not delete that playlist",
  });
  if (!ok) return;
  showToast("Playlist deleted");
  document.dispatchEvent(new CustomEvent(PLAYLIST_DELETED, { detail: { playlistId } }));
  refreshFragments();
}
