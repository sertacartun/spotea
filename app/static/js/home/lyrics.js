// The player panel's second view: timed lyrics that follow the track.
//
// Two rules shape this module.
//
// **Nothing is fetched until the tab is selected.** A miss costs two live
// YouTube requests and, measured, about two thirds of tracks have no lyrics
// at all (see app/services/lyrics.py) — so fetching on play would spend most
// of that request budget on answers nobody asked for. The cost of waiting
// instead is one ~1s wait the first time this tab is opened on a track.
//
// **The audio element is only ever listened to, never touched.** Following
// along is a `timeupdate` consumer and nothing more: no play(), no pause(),
// no src. The player's iOS behaviour is load-bearing and easy to break from
// the outside — see the background-playback notes in player.js.

import { api } from "../core.js";
import { activeAudio, onPlayerEvent } from "../player.js";

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

// After a manual scroll, leave the list where the reader put it for this
// long. Without it, following along drags the view back mid-read every time
// a line changes, which makes reading ahead impossible.
const MANUAL_SCROLL_GRACE_MS = 6000;

let selected = "queue";
// The content id the rendered lyrics belong to, so a track change is
// noticed without needing an event nothing currently fires.
let renderedFor = null;
let loadingFor = null;
let lines = [];
let activeIndex = -1;
let lastManualScrollAt = 0;
// Set while this module is the one scrolling, so the listener below can tell
// its own smooth scroll from a reader's finger. Without it every automatic
// scroll stamped lastManualScrollAt as it animated and locked the *next*
// MANUAL_SCROLL_GRACE_MS out — so following along ran in bursts: several
// lines would go by with the list held still (long enough on a phone for the
// current line to slide off the bottom of a short panel), then one jump to
// catch up, then another six seconds of nothing.
let autoScrolling = false;
// Cleared on `scrollend` where it exists; this is the fallback for a browser
// that has no such event, and the backstop for a scrollTo that lands on the
// position it was already at and therefore never animates or ends.
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
  // The drawer (below 900px) scrolls .queue-panel-inner itself. The desktop
  // layout turns it into a static reference box (overflow: hidden) and makes
  // each tab panel its own absolutely positioned scroller instead — see
  // style.css's ".queue-panel-inner > [role=\"tabpanel\"]" under the
  // min-width: 900px block. Reading the computed style rather than
  // duplicating that breakpoint here is what keeps this right if it ever
  // moves; get it wrong and this scrolls a box with no scrollbar, which is
  // silently a no-op — exactly why the active line stopped following along
  // on desktop.
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
    // Said out loud rather than left blank. "No lyrics" is the common
    // answer here and an empty panel reads as a failure to load.
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

  // No errorMessage: a failure belongs in the panel the reader is looking
  // at, not in a toast over the player.
  const { ok, data } = await api(`/content/${contentId}/lyrics`);

  // The track moved on while this was in flight — whatever came back
  // describes something that is no longer playing.
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
  // Backwards: the answer is almost always the current line or the one
  // after it, so this returns within a step or two of where it started.
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (ms >= lines[i].start_ms) return i;
  }
  return -1;
}

/**
 * Centres a line in the panel.
 *
 * `force` skips the manual-scroll grace period — for the one case that isn't
 * following along: the reader has just switched to this tab and is looking at
 * whatever the list happened to be scrolled to, which may be nowhere near
 * what is currently being sung.
 */
function scrollActiveIntoView(el, { force = false } = {}) {
  const box = scroller();
  if (!box) return;
  if (!force && Date.now() - lastManualScrollAt < MANUAL_SCROLL_GRACE_MS) return;

  // Measured rather than scrollIntoView(), which scrolls every scrollable
  // ancestor including the page behind the overlay.
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

/**
 * Marks the line the track is on and brings it into view.
 *
 * `force` is passed by the tab switch below, which has to move the list even
 * though the line hasn't changed — see showTab.
 */
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

  // Switching views resets the scroll: the two lists have nothing to do with
  // each other, and landing halfway down a set of lyrics is disorienting.
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
  // Already rendered, so nothing is loading and the timeupdate handler only
  // acts when the line *changes* — opening this tab in the middle of a verse
  // would otherwise leave the reader at the top of the song until the next
  // line came round. The scroll above is what makes this necessary and also
  // what makes it safe to force: the list is at the top either way.
  //
  // Deferred a frame: the panel was `hidden` until a moment ago, so it has no
  // measurable height yet and centring against it would compute against zero.
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
      // Only a person scrolling counts. This module's own smooth scroll fires
      // the same event, dozens of times as it animates, and counting those as
      // a reader's finger is what made following along stall for six seconds
      // after every single line change (see autoScrolling).
      if (selected === "lyrics" && !autoScrolling) lastManualScrollAt = Date.now();
    },
    { passive: true }
  );

  // The moment the animation settles, hand the list back. Not universally
  // supported — AUTO_SCROLL_MAX_MS covers the browsers without it.
  box?.addEventListener(
    "scrollend",
    () => {
      autoScrolling = false;
      clearTimeout(autoScrollTimer);
    },
    { passive: true }
  );

  // A different track means different lyrics; the reader's place in the old
  // ones has nothing to do with the new ones.
  onPlayerEvent("loadedmetadata", () => {
    lastManualScrollAt = 0;
  });

  // The only hook into playback: read the clock, move the highlight. Listened
  // to through onPlayerEvent so this never holds a reference to the audio
  // element itself.
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
