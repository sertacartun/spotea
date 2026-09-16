// Play-queue state only; never touches the player — listeners react to QUEUE_CHANGED.
// `ids` (list order) and `shuffleOrder` are both fixed per queue so shuffle can be undone.

import { api } from "../core.js";

export const QUEUE_CHANGED = "spotea:queuechange";

const QUEUE_KEY = "spotea-queue";
// Shuffle/repeat are standing preferences, so localStorage rather than the
// queue's sessionStorage — they must survive closing the app.
const PREFS_KEY = "spotea-play-prefs";

// Persisted: resume.js forces a reload on every bfcache restore (every iOS PWA
// trip to the home screen), which would otherwise drop the queue.
let state = {
  source: null,
  ids: [],
  shuffleOrder: [],
  order: [],
  position: -1,
  shuffle: false,
  repeat: "off",
};

const REPEAT_MODES = ["off", "all", "one"];

function persist() {
  try {
    sessionStorage.setItem(QUEUE_KEY, JSON.stringify(state));
    localStorage.setItem(PREFS_KEY, JSON.stringify({ shuffle: state.shuffle, repeat: state.repeat }));
  } catch (err) {
    /* Storage unavailable (private browsing) — the queue just won't outlive the page. */
  }
}

function restore() {
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem(QUEUE_KEY) || "null");
  } catch (err) {
    return;
  }
  // Shape-checked: a stale or half-written record must not crash nextId().
  if (!saved || !Array.isArray(saved.ids) || !Array.isArray(saved.order)) return;
  state = {
    source: saved.source ?? null,
    ids: saved.ids,
    shuffleOrder: Array.isArray(saved.shuffleOrder) ? saved.shuffleOrder : shuffled(saved.ids),
    order: saved.order,
    position: Number.isInteger(saved.position) ? saved.position : -1,
    shuffle: saved.shuffle === true,
    repeat: REPEAT_MODES.includes(saved.repeat) ? saved.repeat : "off",
  };
}

/** Runs after restore() to override its copy; skipped when there's no record so
 *  blocked localStorage doesn't clobber the queue's own copy with defaults. */
function restorePrefs() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null");
  } catch (err) {
    return;
  }
  if (!saved) return;
  state.shuffle = saved.shuffle === true;
  state.repeat = REPEAT_MODES.includes(saved.repeat) ? saved.repeat : "off";
}

restore();
const queueShuffle = state.shuffle;
restorePrefs();
// Records can disagree across tabs. Rebuild only then: unconditionally would
// reshuffle up-next on every reload, and iOS PWAs reload on every return.
if (state.ids.length && state.shuffle !== queueShuffle) {
  applyOrder(state.order[state.position] ?? null);
}

function announce() {
  persist();
  document.dispatchEvent(new CustomEvent(QUEUE_CHANGED));
}

function shuffled(ids) {
  const out = ids.slice();
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

/** Switches between the queue's two fixed orders, keeping the pointer on `keepId`. */
function applyOrder(keepId) {
  state.order = (state.shuffle ? state.shuffleOrder : state.ids).slice();
  state.position = keepId == null ? -1 : state.order.indexOf(keepId);
}

function queueUrl(source) {
  if (source.kind === "user-playlist") return `/content/queue/user-playlist/${source.id}`;
  return `/content/queue/playlist/${source.kind}`;
}

export function queueSource() {
  return state.source;
}

/** Index `offset` steps away, or null. Under repeat "all" the ends join up. */
function peekIndex(offset) {
  if (state.position < 0 || !state.order.length) return null;
  const index = state.position + offset;
  if (index >= 0 && index < state.order.length) return index;
  if (state.repeat !== "all") return null;
  // A queue of one must not wrap onto itself (`1 % 1` is 0 would restart it).
  if (state.order.length < 2) return null;
  return ((index % state.order.length) + state.order.length) % state.order.length;
}

function peek(offset) {
  const index = peekIndex(offset);
  return index === null ? null : state.order[index];
}

/** Read-only lookahead; must never advance playback. */
export function peekNextId() {
  return peek(1);
}

export function peekPreviousId() {
  return peek(-1);
}

export function isShuffled() {
  return state.shuffle;
}

export function repeatMode() {
  return state.repeat;
}

/** The whole queue in play order, including already-played tracks, so panel rows don't move. */
export function queueOrder() {
  return state.order.slice();
}

export function currentId() {
  return state.position < 0 ? null : (state.order[state.position] ?? null);
}

/** off -> all -> one -> off. "one" doesn't affect the Next button, only a track ending. */
export function cycleRepeat() {
  const next = (REPEAT_MODES.indexOf(state.repeat) + 1) % REPEAT_MODES.length;
  state.repeat = REPEAT_MODES[next];
  announce();
  return state.repeat;
}

function step(offset) {
  const index = peekIndex(offset);
  if (index === null) return null;
  state.position = index;
  announce();
  return state.order[index];
}

export function nextId() {
  return step(1);
}

export function previousId() {
  return step(-1);
}

/**
 * Called for every track the player opens: a track inside the queue moves the
 * pointer; one from elsewhere drops the queue so it can't advance into it.
 */
export function noteCurrent(contentId) {
  const id = Number(contentId);
  const index = state.order.indexOf(id);
  if (index === -1) {
    if (state.order.length) clearQueue();
    return;
  }
  if (index === state.position) return;
  state.position = index;
  announce();
}

export function clearQueue() {
  state = {
    source: null,
    ids: [],
    shuffleOrder: [],
    order: [],
    position: -1,
    shuffle: state.shuffle,
    repeat: state.repeat,
  };
  announce();
}

/** A standing preference: holds with no queue loaded and shapes the next loadQueue(). */
export function toggleShuffle() {
  state.shuffle = !state.shuffle;
  if (state.order.length) applyOrder(state.order[state.position] ?? null);
  announce();
  return state.shuffle;
}

/**
 * `startId` is the track already playing; it stays current (and first under
 * shuffle). Returns the id to start, or null.
 */
export async function loadQueue(source, { startId = null } = {}) {
  const { ok, data } = await api(queueUrl(source), { errorMessage: "Could not load the queue" });
  if (!ok || !data?.ids?.length) return null;
  return setQueue(source, data.ids, { startId });
}

/** loadQueue with the ids already in hand (remote lists have no queue endpoint). */
export function setQueue(source, ids, { startId = null } = {}) {
  if (!ids?.length) return null;

  state.source = { kind: source.kind, id: source.id ?? null };
  state.ids = ids;
  // Number(): startId comes from dataset (string), ids are JSON numbers.
  const start = startId == null ? null : Number(startId);
  const keep = state.ids.includes(start) ? start : null;
  // A clicked track goes to the front of the shuffle so the rest of the list follows it.
  state.shuffleOrder = keep == null ? shuffled(state.ids) : [keep, ...shuffled(state.ids.filter((id) => id !== keep))];
  applyOrder(keep);
  if (state.position === -1) state.position = 0;
  announce();
  return state.order[state.position];
}
