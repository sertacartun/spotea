// Timed lyrics panel. Fetched only when the tab is selected (most tracks miss, and
// misses cost live requests); the audio element is only listened to, never touched.

import { api } from "../core.js";
import { activeAudio, onPlayerEvent } from "../player.js";

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const MANUAL_SCROLL_GRACE_MS = 6000;

let selected = "queue";
let renderedFor = null;
let loadingFor = null;
let lines = [];
let activeIndex = -1;
let lastManualScrollAt = 0;
// Tells this module's own smooth scroll apart from a reader's, which starts the grace period.
let autoScrolling = false;
// Fallback for browsers without `scrollend`, and for a scrollTo that never animates.
const AUTO_SCROLL_MAX_MS = 800;
let autoScrollTimer = null;

function playerContentId() {
  return document.getElementById("player-root")?.dataset.contentId || "";
}

function body() {
  return document.getElementById("lyrics-panel-body");
}

function scroller() {
  const inner = document.querySelector(".queue-panel-inner");
  if (!inner) return null;
  // Below 900px .queue-panel-inner scrolls; on desktop each tab panel does. Reading the
  // computed style avoids duplicating the breakpoint — the wrong box silently no-ops.
  return getComputedStyle(inner).overflowY === "auto" ? inner : body();
}

function setMessage(text) {
  const el = body();
  if (el) el.innerHTML = `<p class="lyrics-empty"></p>`;
  if (el) el.querySelector(".lyrics-empty").textContent = text;
}

function render(payload) {
  const el = body();
  if (!el) return;
  lines = payload.lines || [];
  activeIndex = -1;

  if (!lines.length) {
    setMessage("No lyrics for this track.");
    return;
  }

  el.innerHTML = "";
  for (const line of lines) {
    const p = document.createElement("p");
    p.className = "lyrics-line";
    // textContent, not innerHTML: this string comes from YouTube Music.
    p.textContent = line.text;
    el.append(p);
  }
  if (payload.source) {
    const credit = document.createElement("p");
    credit.className = "lyrics-source";
    credit.textContent = payload.source;
    el.append(credit);
  }
}

async function load(contentId) {
  if (!contentId || loadingFor === contentId) return;
  loadingFor = contentId;
  renderedFor = contentId;
  lines = [];
  activeIndex = -1;
  setMessage("Loading lyrics…");

  // No errorMessage: failures show in the panel, not as a toast over the player.
  const { ok, data } = await api(`/content/${contentId}/lyrics`);

  if (playerContentId() !== contentId) {
    loadingFor = null;
    return;
  }
  loadingFor = null;

  if (!ok) {
    renderedFor = null; // so selecting the tab again retries
    setMessage("Couldn't load lyrics.");
    return;
  }
  render(data);
}

/** The line covering this moment, or -1 before the first one starts. */
function indexAt(ms) {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (ms >= lines[i].start_ms) return i;
  }
  return -1;
}

/** Centres a line in the panel; `force` skips the manual-scroll grace period. */
function scrollActiveIntoView(el, { force = false } = {}) {
  const box = scroller();
  if (!box) return;
  if (!force && Date.now() - lastManualScrollAt < MANUAL_SCROLL_GRACE_MS) return;

  // Not scrollIntoView(): it would also scroll the page behind the overlay.
  const boxRect = box.getBoundingClientRect();
  const elRect = el.getBoundingClientRect();
  const top = box.scrollTop + (elRect.top - boxRect.top) - (box.clientHeight - elRect.height) / 2;

  autoScrolling = true;
  clearTimeout(autoScrollTimer);
  autoScrollTimer = setTimeout(() => {
    autoScrolling = false;
  }, AUTO_SCROLL_MAX_MS);

  box.scrollTo({ top, behavior: reducedMotion.matches ? "auto" : "smooth" });
}

function syncActiveLine({ force = false } = {}) {
  const seconds = activeAudio()?.currentTime;
  if (typeof seconds !== "number") return;

  const index = indexAt(Math.round(seconds * 1000));
  if (index === activeIndex && !force) return;

  const rendered = body()?.querySelectorAll(".lyrics-line") || [];
  if (index !== activeIndex) rendered[activeIndex]?.classList.remove("is-active");
  activeIndex = index;
  const el = rendered[index];
  if (!el) return;
  el.classList.add("is-active");
  scrollActiveIntoView(el, { force });
}

function showTab(name) {
  selected = name;
  const isLyrics = name === "lyrics";
  for (const [tab, panel, on] of [
    ["panel-tab-queue", "queue-panel-body", !isLyrics],
    ["panel-tab-lyrics", "lyrics-panel-body", isLyrics],
  ]) {
    const tabEl = document.getElementById(tab);
    const panelEl = document.getElementById(panel);
    if (tabEl) {
      tabEl.classList.toggle("is-selected", on);
      tabEl.setAttribute("aria-selected", String(on));
    }
    if (panelEl) panelEl.hidden = !on;
  }

  scroller()?.scrollTo({ top: 0 });

  if (!isLyrics) return;
  const contentId = playerContentId();
  if (contentId && contentId !== renderedFor) {
    load(contentId);
    return;
  }
  if (!contentId) {
    setMessage("Nothing playing.");
    return;
  }
  // Forced because the line hasn't changed, so timeupdate won't scroll to it. Deferred
  // a frame: the panel was `hidden` until now and has no measurable height yet.
  if (lines.length) requestAnimationFrame(() => syncActiveLine({ force: true }));
}

export function setupLyricsPanel() {
  const queueTab = document.getElementById("panel-tab-queue");
  const lyricsTab = document.getElementById("panel-tab-lyrics");
  if (!queueTab || !lyricsTab) return;

  queueTab.addEventListener("click", () => showTab("queue"));
  lyricsTab.addEventListener("click", () => showTab("lyrics"));

  const box = scroller();
  box?.addEventListener(
    "scroll",
    () => {
      if (selected === "lyrics" && !autoScrolling) lastManualScrollAt = Date.now();
    },
    { passive: true }
  );

  box?.addEventListener(
    "scrollend",
    () => {
      autoScrolling = false;
      clearTimeout(autoScrollTimer);
    },
    { passive: true }
  );

  onPlayerEvent("loadedmetadata", () => {
    lastManualScrollAt = 0;
  });

  // Via onPlayerEvent so this module never holds a reference to the audio element.
  onPlayerEvent("timeupdate", () => {
    if (selected !== "lyrics") return;
    const contentId = playerContentId();
    if (contentId && contentId !== renderedFor) {
      load(contentId);
      return;
    }
    if (lines.length) syncActiveLine();
  });
}
