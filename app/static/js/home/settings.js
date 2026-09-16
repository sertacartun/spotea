import { api, confirmDialog, setupOverlay, showToast } from "../core.js";
import { refreshDownloadsBody, refreshFragments } from "../fragments.js";
import { clearAll as clearDeviceCopies, deleteTrack } from "../offline.js";
import { syncDeviceSummary } from "./device.js";
import { reloadRecommendations } from "./explore.js";
import { activate } from "./tabs.js";

export function setupDownloadsOverlay() {
  setupOverlay("downloads-overlay", "downloads-close", ["open-downloads"]);
  // The list is server-rendered; this just freshens it after opening without blocking.
  document.getElementById("open-downloads")?.addEventListener("click", () => {
    refreshDownloadsBody();
  });
}

function clearDownloadsPrompt() {
  const total = document.getElementById("storage-total")?.textContent.trim();
  const scale = total ? ` (${total})` : "";
  const onDevice = Number(document.getElementById("device-summary-text")?.dataset.count) > 0;
  const device = onDevice ? " Anything kept on this device goes too." : "";
  return (
    `Delete every downloaded file${scale}?${device} ` +
    "Your artists and saved songs stay, and anything you play downloads again."
  );
}

/** Confirm, call, re-render. `alsoDownloads` is needed from inside the Downloads
    modal: refreshFragments() alone doesn't touch that modal's list. */
async function confirmedAction(
  message,
  confirmLabel,
  url,
  { method, errorMessage },
  { alsoDownloads = false, alsoDevice = null } = {}
) {
  if (!(await confirmDialog(message, confirmLabel))) return;
  const { ok } = await api(url, { method, errorMessage });
  if (!ok) return;
  // Drop the device copy too, or its bytes are stranded with no UI handle left.
  if (alsoDevice) {
    await alsoDevice().catch(() => {});
    syncDeviceSummary();
  }
  // Not a reload: that would close the Downloads modal under the user.
  refreshFragments();
  if (alsoDownloads) refreshDownloadsBody();
}

export function setupStorage() {
  document.getElementById("clear-recently-played")?.addEventListener("click", () =>
    confirmedAction(
      "Clear your recently played history? This only affects the Home shelf — nothing gets deleted.",
      "Clear",
      "/content/recently-played",
      { method: "DELETE", errorMessage: "Could not clear recently played" }
    )
  );

  // Delegated from #downloads-body: refreshFragments() replaces its children
  // wholesale, so listeners on #clear-storage/#storage-list would go stale.
  document.getElementById("downloads-body")?.addEventListener("click", (event) => {
    if (event.target.closest("#clear-storage")) {
      confirmedAction(
        clearDownloadsPrompt(),
        "Clear all",
        "/storage",
        { method: "DELETE", errorMessage: "Could not clear downloads" },
        { alsoDownloads: true, alsoDevice: clearDeviceCopies }
      );
      return;
    }

    const removeBtn = event.target.closest(".storage-remove");
    if (removeBtn) {
      const contentId = removeBtn.dataset.contentId;
      confirmedAction(
        "Remove this download? You can get it back by playing it again.",
        "Remove",
        `/content/${contentId}`,
        { method: "DELETE", errorMessage: "Could not remove this download" },
        { alsoDownloads: true, alsoDevice: () => deleteTrack(contentId) }
      );
    }
  });
}

// PUT whole on every change; the server normalizes it, so chips re-sync from the response.
let interests = [];

function pickerChips() {
  return [...document.querySelectorAll("#interests-picker .genre-chip")];
}

function syncChips() {
  const on = new Set(interests.map((interest) => interest.toLowerCase()));
  for (const chip of pickerChips()) {
    const selected = on.has(chip.dataset.genre.toLowerCase());
    chip.setAttribute("aria-pressed", String(selected));
    chip.classList.toggle("is-on", selected);
  }
  syncDoneButton();
}

/** Enable Continue only once the minimum is met. Driven from syncChips so it follows
 *  save corrections and roll-backs — a first run must not close onto an empty list. */
function syncDoneButton() {
  const done = document.getElementById("interests-done");
  const picker = document.getElementById("interests-picker");
  if (!done || !picker) return;
  const minimum = Number(picker.dataset.minInterests) || 0;
  const remaining = minimum - interests.length;
  done.disabled = remaining > 0;
  const note = document.getElementById("interests-remaining");
  if (note) note.textContent = remaining > 0 ? `Pick ${remaining} more` : "";
}

// Saves are serialized: every PUT carries the whole list, so overlapping ones can
// land out of order and silently undo the later toggle.
let pendingSave = Promise.resolve();

export function saveInterests(next, errorMessage) {
  const previous = interests;
  // Optimistic; reverted below if the save doesn't land.
  interests = next;
  syncChips();

  pendingSave = pendingSave.then(async () => {
    const { ok, data } = await api("/settings", {
      method: "PUT",
      body: { interests: next },
      errorMessage,
    });

    // A newer edit's queued save owns the chips now (identity check: each edit is a fresh array).
    if (next !== interests) return;

    interests = ok ? data.interests : previous;
    syncChips();
    // Rebuild Explore in the background now rather than on next open (several live searches).
    if (ok) reloadRecommendations();
  });
  return pendingSave;
}

export function setupInterests() {
  const picker = document.getElementById("interests-picker");
  if (!picker) return;

  // Serves both Settings' "Manage interests" and the first run (data-required).
  const overlay = setupOverlay("interests-overlay", "interests-close", ["open-interests"]);

  interests = pickerChips()
    .filter((chip) => chip.getAttribute("aria-pressed") === "true")
    .map((chip) => chip.dataset.genre);
  syncDoneButton();

  picker.addEventListener("click", (event) => {
    const chip = event.target.closest(".genre-chip");
    if (!chip) return;
    const genre = chip.dataset.genre;
    const on = chip.getAttribute("aria-pressed") === "true";
    saveInterests(
      on
        ? interests.filter((interest) => interest.toLowerCase() !== genre.toLowerCase())
        : [...interests, genre],
      on ? "Could not remove that interest" : "Could not save your interests"
    );
  });

  document.getElementById("interests-done")?.addEventListener("click", () => {
    // isRequired() in core.js consults this; leaving it would trap Manage interests later.
    document.getElementById("interests-overlay")?.removeAttribute("data-required");
    overlay?.close();

    activate("explore");
    refreshFragments();
  });
}

export function setupSettings() {
  const qualitySelect = document.getElementById("audio-quality-select");
  qualitySelect?.addEventListener("change", () => {
    api("/settings", {
      method: "PUT",
      body: { audio_quality: qualitySelect.value },
      errorMessage: "Could not update audio quality",
    });
  });

}
