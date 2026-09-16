import { api, confirmDialog, escapeHtml, promptDialog, setupOverlay, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";

/** Announced as events, not acted on: detail.js owns the panel and importing it would be a cycle. */
export const PLAYLIST_CHANGED = "spotea:playlist-changed";
export const PLAYLIST_DELETED = "spotea:playlist-deleted";

let pickerContentId = null;

/** Resolves to the new playlist, or null when dismissed. */
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

  if (status === 409) return createPlaylist("You already have a playlist called that — try another name.");
  showToast("Could not create that playlist");
  return null;
}

function renderPicker(playlists) {
  const list = document.getElementById("playlist-picker-list");
  if (!list) return;

  if (!playlists.length) {
    list.innerHTML =
      '<li class="playlist-picker-empty muted">No playlists yet — make one below.</li>';
    return;
  }

  // `isIn` stays a bare identifier: test_escape_html_is_the_only_escaper_used_in_markup
  // only accepts bare interpolations, and a boolean stringifies to what aria-pressed wants.
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
  await loadPicker();
}

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

  showToast(isIn ? "Removed from playlist" : data?.status === "duplicate" ? "Already in that playlist" : "Added to playlist");
  await loadPicker();
  refreshFragments();
}

export function setupPlaylists() {
  setupOverlay("playlist-picker-overlay", "playlist-picker-close");

  document.getElementById("add-to-playlist-btn")?.addEventListener("click", openPicker);

  // Delegated: the list's rows are rewritten on every change.
  document.getElementById("playlist-picker-overlay")?.addEventListener("click", (event) => {
    const row = event.target.closest(".playlist-picker-row");
    if (row) {
      togglePlaylistMembership(row);
      return;
    }
    if (event.target.closest("#playlist-picker-new")) {
      createPlaylist().then((playlist) => {
        if (!playlist) return;
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

  // #library-grid is the swap target, not a swapped node, so this listener survives refreshes.
  document.getElementById("library-grid")?.addEventListener("click", (event) => {
    if (!event.target.closest("#new-playlist-btn")) return;
    createPlaylist().then((playlist) => {
      if (playlist) refreshFragments();
    });
  });

  // Separate from detail.js's listener, which routes via ".track-row .track-link" and can't reach these.
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
  // Counts are server-rendered, so the panel is re-opened rather than patched in the DOM.
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
