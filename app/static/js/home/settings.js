import { api, confirmDialog, setupOverlay, showToast } from "../core.js";
import { onFragmentsSwapped, refreshFragments } from "../fragments.js";
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

const EXPORT_FILENAME = "spotea-downloads.zip";
const EXPORT_LABEL = "Export all";
const EXPORT_READY_LABEL = "Share export";

// A same-window navigation to a zip download traps iOS home-screen installs in a
// full-screen "Open in..." view with no way back (WebKit bug 236943), so the zip goes to
// the share sheet instead, which iOS can actually dismiss.
//
// But iOS only runs navigator.share() while the tap that asked for it still counts as
// user activation — a few seconds — and building and fetching a multi-song zip takes
// longer than that. Sharing straight after the fetch therefore failed with
// NotAllowedError. So the first tap only prepares the file; the second one shares it with
// no await in front of the share() call.
let preparedExport = null;

function canShareFiles() {
  // Probed with an empty file of the same type: there is nothing to share yet.
  return Boolean(navigator.canShare?.({ files: [new File([], EXPORT_FILENAME, { type: "application/zip" })] }));
}

function markExportReady(button) {
  button.textContent = EXPORT_READY_LABEL;
  button.classList.add("is-ready");
}

function resetExportButton(button) {
  preparedExport = null;
  button.textContent = EXPORT_LABEL;
  button.classList.remove("is-ready");
}

/** The zip as a File, or null when it couldn't be built (the toast is already up). */
async function fetchExport() {
  let res;
  try {
    res = await fetch("/storage/export");
  } catch {
    showToast("Could not build the export");
    return null;
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    showToast(data?.detail || "Could not build the export");
    return null;
  }
  return new File([await res.blob()], EXPORT_FILENAME, { type: "application/zip" });
}

function saveExport(file) {
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = EXPORT_FILENAME;
  link.click();
  URL.revokeObjectURL(url);
}

/** Nothing may be awaited before share(): an await here is what spends the activation. */
async function shareExport(button) {
  const file = preparedExport;
  try {
    await navigator.share({ files: [file] });
  } catch (err) {
    // AbortError: the share sheet was dismissed. Either way the file stays ready for another tap.
    if (err.name === "AbortError") return;
    showToast(
      err.name === "NotAllowedError" ? "Tap Share export again" : "Could not share the export"
    );
    return;
  }
  resetExportButton(button);
}

async function exportDownloads(button) {
  if (preparedExport) {
    await shareExport(button);
    return;
  }

  const sharing = canShareFiles();
  button.disabled = true;
  button.textContent = sharing ? "Preparing…" : "Exporting…";
  try {
    const file = await fetchExport();
    if (!file) {
      button.textContent = EXPORT_LABEL;
      return;
    }
    if (!sharing) {
      saveExport(file);
      button.textContent = EXPORT_LABEL;
      return;
    }
    preparedExport = file;
    markExportReady(button);
  } finally {
    button.disabled = false;
  }
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

  // Delegated: the storage fragment swap replaces #export-downloads.
  document.getElementById("settings-downloads-actions")?.addEventListener("click", (event) => {
    const button = event.target.closest("#export-downloads");
    if (button) exportDownloads(button);
  });

  // A refresh re-renders the button from the server, which doesn't know a zip is waiting.
  onFragmentsSwapped(() => {
    const button = document.getElementById("export-downloads");
    if (preparedExport && button) markExportReady(button);
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
