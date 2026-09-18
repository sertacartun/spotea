import { showUpdateBanner } from "./core.js";

const RESUME_KEY = "spotea-resume";

// bfcache restores stale server-rendered DOM without contacting the server, so force a real reload.
export function installBfcacheReload() {
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    saveResumeState();
    window.location.reload();
  });

  // pagehide covers departures that never come back via bfcache (e.g. logout, then a fresh load).
  window.addEventListener("pagehide", saveResumeState);
}

// Assigning audio.src restarts at 0:00 and iOS PWAs reload constantly in the background,
// so position is snapshotted here and restored by player.js / home/overlay.js.
function saveResumeState() {
  const audio = document.getElementById("audio");
  const root = document.getElementById("player-root");
  const contentId = root?.dataset.contentId;
  if (!audio || !contentId) return;
  // No src means still downloading: paused is incidental, and saving wasPlaying: false
  // would suppress the auto-play once the download finishes.
  if (!audio.src) return;
  try {
    // Defaulting to collapsed: landing on the SPA with something playing shows only the mini bar.
    const overlay = document.getElementById("player-overlay");
    const wasExpanded = overlay ? !overlay.hidden : false;
    sessionStorage.setItem(
      RESUME_KEY,
      JSON.stringify({ contentId, currentTime: audio.currentTime, wasPlaying: !audio.paused, wasExpanded })
    );
  } catch (err) {
    /* sessionStorage unavailable (e.g. private browsing) — losing the resume position is harmless. */
  }
}

/** Does not clear it — see consumeResumeState. */
export function readResumeState() {
  try {
    const raw = sessionStorage.getItem(RESUME_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

export function clearResumeState() {
  try {
    sessionStorage.removeItem(RESUME_KEY);
  } catch (err) {
    /* sessionStorage unavailable — nothing to clean up. */
  }
}

/** Consumed on read so a stale record can never apply to a later, unrelated load. */
export function consumeResumeState(contentId) {
  const saved = readResumeState();
  if (saved === null) return null;
  clearResumeState();
  return saved.contentId === contentId ? saved : null;
}

// Registered from every page so install works anywhere. An installed PWA can run for days
// without navigating, so updates must be polled explicitly.
export function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;

  // A first-ever install also fires controllerchange (clients.claim()); that one means nothing.
  const hadController = Boolean(navigator.serviceWorker.controller);
  // This used to reload here. A reload mid-track assigns audio.src again and iOS reads the
  // teardown as the page being done with audio, so it stops playing and clears Now Playing —
  // for an update nobody asked for. The banner offers the reload instead.
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController) showUpdateBanner();
  });

  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .then((registration) => {
        // Also on every return to foreground, the only regular event an installed PWA gets.
        const check = () => registration.update().catch(() => {});
        check();
        document.addEventListener("visibilitychange", () => {
          if (!document.hidden) check();
        });
      })
      .catch(() => {
        // Not fatal — the app works the same without it, just not installable.
      });
  });
}
