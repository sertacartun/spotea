import { api, confirmDialog } from "./core.js";

/** Confirm, then unfollow. `onConfirmed` fires before the request so callers can cover the UI. */
export async function unfollowArtist(artistId, onConfirmed) {
  const confirmed = await confirmDialog(
    "Unfollow this artist? Their songs will be removed from your library.",
    "Unfollow"
  );
  if (!confirmed) return false;

  if (onConfirmed) onConfirmed();

  const { ok } = await api(`/artists/${artistId}`, {
    method: "DELETE",
    errorMessage: "Could not unfollow this channel",
  });
  return ok;
}
