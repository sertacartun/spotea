import { api, confirmDialog, setupOverlay } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { reloadRecommendations } from "./explore.js";
import { activate } from "./tabs.js";

async function clearCache() {
  const size = document.getElementById("settings-cache-desc")?.textContent.split(" across ")[0].trim();
  const confirmed = await confirmDialog(
    `Delete cached songs${size ? ` (${size})` : ""}? These are songs you played but didn't download; ` +
      "they download again when you play them. Your downloads stay.",
    "Clear cache"
  );
  if (!confirmed) return;
  const { ok } = await api("/storage/cache", { method: "DELETE", errorMessage: "Could not clear the cache" });
  if (ok) refreshFragments();
}

export function setupStorage() {
  document.getElementById("clear-recently-played")?.addEventListener("click", async () => {
    const confirmed = await confirmDialog(
      "Clear your recently played history? This only affects the Home shelf — nothing gets deleted.",
      "Clear"
    );
    if (!confirmed) return;
    const { ok } = await api("/content/recently-played", {
      method: "DELETE",
      errorMessage: "Could not clear recently played",
    });
    if (ok) refreshFragments();
  });

  // Delegated: the storage fragment swap replaces #clear-cache.
  document.getElementById("settings-cache-actions")?.addEventListener("click", (event) => {
    if (event.target.closest("#clear-cache")) clearCache();
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
