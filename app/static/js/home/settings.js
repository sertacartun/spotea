// The Settings tab's controls, plus the Downloads modal they open.

import { api, confirmDialog, setupOverlay, showToast } from "../core.js";
import { refreshDownloadsBody, refreshFragments } from "../fragments.js";
import { clearAll as clearDeviceCopies, deleteTrack } from "../offline.js";
import { syncDeviceSummary } from "./device.js";
import { reloadRecommendations } from "./explore.js";
import { activate } from "./tabs.js";

export function setupDownloadsOverlay() {
  setupOverlay("downloads-overlay", "downloads-close", ["open-downloads"]);
  // The modal already has *something* to show the instant it opens — its
  // list was server-rendered at page load — so this doesn't block that open
  // on a round trip, just freshens it right after in case anything changed
  // since (another tab, the background refresh finishing a download).
  document.getElementById("open-downloads")?.addEventListener("click", () => {
    refreshDownloadsBody();
  });
}

/** What "Clear all" is about to delete, in the numbers already on screen.
 *
 *  A confirmation that only says "delete all downloaded audio?" leaves the
 *  user to remember how much that is. The summary line right above the button
 *  knows — it is rendered from the same usage figures (see _downloads.html) —
 *  so the prompt quotes it back, and then says what *isn't* going away, which
 *  is the part that actually decides the answer. */
function clearDownloadsPrompt() {
  const total = document.getElementById("storage-total")?.textContent.trim();
  const scale = total ? ` (${total})` : "";
  // The device's own copies are described in Settings now, not in this modal
  // (see index.html's "Songs on this device"), but they are still what this
  // button deletes — so the count is read from where it lives.
  const onDevice = Number(document.getElementById("device-summary-text")?.dataset.count) > 0;
  // Only said when there is something to say. The device copies are the ones
  // that survive having no signal, so a prompt that let them go silently
  // would be the most expensive thing on this screen to get wrong.
  const device = onDevice ? " Anything kept on this device goes too." : "";
  return (
    `Delete every downloaded file${scale}?${device} ` +
    "Your artists and saved songs stay, and anything you play downloads again."
  );
}

/** Confirm, then call, then re-render whatever it changed. `alsoDownloads`
    is for the two actions below that run *from inside* the open Downloads
    modal (clear all, remove one) — refreshFragments() alone no longer
    touches that modal's list (see fragments.js), so without this a user's
    own action wouldn't appear to do anything until they closed and reopened
    it. */
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
  // The device's own copy of whatever the server just dropped. Removing a
  // download is the user saying they want it gone, and the row it was on is
  // about to disappear from this list — leaving the device copy behind would
  // strand bytes with no remaining handle in the UI to delete them by.
  if (alsoDevice) {
    await alsoDevice().catch(() => {});
    // The Settings line and Library's tile both count what is on the device,
    // and one of them just stopped being true.
    syncDeviceSummary();
  }
  // A fragment refresh rather than a reload: these run from inside the
  // Downloads modal, and reloading closed it out from under the user.
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

  // Delegated from #downloads-body, not #clear-storage/#storage-list directly:
  // refreshFragments() (see fragments.js) rewrites this whole region's
  // children — including #clear-storage and #storage-list themselves —
  // wholesale on every refresh (e.g. after any download, from player.js), so
  // listeners on those elements go stale as soon as one happens. #downloads-body
  // is the fragment's swap target, not one of the swapped nodes, so it's the
  // only element here guaranteed to still be in the DOM.
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

// The interests Explore's recommendations are searched from. Held here as
// one array and PUT whole on every change (see schemas.SettingsUpdate) — the
// server normalizes what it stores (trimming, deduping, capping), so the
// response, not this array, is what the chips are re-synced from.
//
// The chips themselves are server-rendered (see _interest_picker.html), which
// is the same partial Home's first-run panel uses. There used to be a
// free-text chip list and an add form here instead: a second editor of one
// field, which looked nothing like the genre picker and did the same job.
let interests = [];

function pickerChips() {
  return [...document.querySelectorAll("#interests-picker .genre-chip")];
}

/** Push `interests` back onto the chips. The source of truth after a save is
 *  the server's normalized list, which can differ from what was sent — an
 *  over-long or duplicate tag comes back cleaned up rather than rejected. */
function syncChips() {
  const on = new Set(interests.map((interest) => interest.toLowerCase()));
  for (const chip of pickerChips()) {
    const selected = on.has(chip.dataset.genre.toLowerCase());
    chip.setAttribute("aria-pressed", String(selected));
    chip.classList.toggle("is-on", selected);
  }
  syncDoneButton();
}

/** How many more are needed before the first run will let go, reflected on
 *  the Continue button and the line beside it.
 *
 *  Called from syncChips rather than from the click handler, so it follows
 *  the same list the chips do — including the correction after a save comes
 *  back with a normalized list, and the roll-back after one that failed.
 *  A Continue that stayed enabled on a save that didn't land would close the
 *  overlay onto an empty interest list, which is the one outcome the whole
 *  first run exists to prevent. */
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

// Saves run one after another rather than overlapping. Every PUT carries the
// whole list, so two in flight at once can land in either order — toggling
// two chips in quick succession left the server holding the earlier list, and
// the second response then re-synced the chips to match it, silently undoing
// the change.
let pendingSave = Promise.resolve();

export function saveInterests(next, errorMessage) {
  const previous = interests;
  // Applied before the request is even sent so a chip responds instantly;
  // reverted below if the save doesn't land.
  interests = next;
  syncChips();

  pendingSave = pendingSave.then(async () => {
    const { ok, data } = await api("/settings", {
      method: "PUT",
      body: { interests: next },
      errorMessage,
    });

    // Both branches below only act while this edit is still the current
    // state (each edit assigns a fresh array, so identity is the test). If
    // something has been toggled since, that edit's own queued save owns what
    // the chips should say — adopting this response's list, or rolling back
    // to this edit's predecessor, would undo it.
    if (next !== interests) return;

    interests = ok ? data.interests : previous;
    syncChips();
    // Explore's shelves were searched from the old list, and so was the
    // batch the server has cached — both are now answers to a question
    // nobody asked. Rebuilt right here, in the background, rather than
    // marked stale for whoever opens Explore next to wait on: the rebuild is
    // several live YouTube searches, and this is the moment it can happen
    // without anyone sitting in front of it. Not awaited — the chips above
    // are what this save owes the user.
    if (ok) reloadRecommendations();
  });
  return pendingSave;
}

export function setupInterests() {
  const picker = document.getElementById("interests-picker");
  if (!picker) return;

  // One overlay for both jobs — Settings' "Manage interests" and, on a brand
  // new profile, the first run (see index.html's data-required). It used to
  // be this modal plus an inline panel on Home with its own template, its own
  // module and its own batch-save; the panel is gone.
  //
  // A modal rather than an inline Settings editor for the original reason: a
  // wrapping grid of chips doesn't fit the label-left/control-right shape
  // every other Settings row has, and this way the row looks like its
  // neighbours.
  const overlay = setupOverlay("interests-overlay", "interests-close", ["open-interests"]);

  // Read off the markup rather than fetched on boot or handed over as JSON,
  // because the chips already carry it: the server rendered which ones are
  // on (see interests.interest_chips), and a second copy of the same fact
  // would only be something to keep in step.
  interests = pickerChips()
    .filter((chip) => chip.getAttribute("aria-pressed") === "true")
    .map((chip) => chip.dataset.genre);
  syncDoneButton();

  // Bound to the picker, not to each chip: the chip set is fixed for the life
  // of the page (this overlay is not one of the regions refreshFragments
  // replaces), but one listener is still the honest shape for this and cannot
  // be double-bound the way per-element handlers were on Home.
  picker.addEventListener("click", (event) => {
    const chip = event.target.closest(".genre-chip");
    if (!chip) return;
    const genre = chip.dataset.genre;
    const on = chip.getAttribute("aria-pressed") === "true";
    // No Save button, on a first run as much as afterwards: a toggle is the
    // whole edit, and one that needed confirming would be the second thing
    // this screen asks for after the free-text form was removed for asking
    // the first. Continue below is therefore not a save — by the time it can
    // be pressed, everything it would have saved is already saved.
    saveInterests(
      on
        ? interests.filter((interest) => interest.toLowerCase() !== genre.toLowerCase())
        : [...interests, genre],
      on ? "Could not remove that interest" : "Could not save your interests"
    );
  });

  document.getElementById("interests-done")?.addEventListener("click", () => {
    // Clearing this first is what turns the overlay back into an ordinary
    // one — the close button, the backdrop and Escape all consult it (see
    // core.js's isRequired). Without it, opening Manage interests later in
    // the same session would trap the user in a screen they had already
    // finished with.
    document.getElementById("interests-overlay")?.removeAttribute("data-required");
    overlay?.close();

    // Explore is the payoff: its shelves are searched from exactly what was
    // just picked. The rebuild itself is already running — every toggle
    // queued one (see saveInterests) — so this only goes and looks at it.
    // refreshFragments because Home is still showing the empty-library copy
    // it was rendered with.
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
