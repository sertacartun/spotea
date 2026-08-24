// The play queue: what "Play all" builds, what the player's previous/next
// controls move through, and what a finished track advances into.
//
// State only — this module never touches the player. Everything that reacts
// to a queue change (home/overlay.js's transport buttons and auto-advance,
// home/detail.js's shuffle button) listens for the QUEUE_CHANGED event
// below instead of being called directly. That's what keeps the dependency
// one-way: queue.js is imported by the player and the detail panel, and
// imports neither.
//
// A queue holds two fixed orderings and a pointer at one of them. `ids` is
// the channel/playlist's own order, exactly as its track list shows it;
// `shuffleOrder` is the one shuffle of it this queue will ever have; `order`
// is whichever of the two is in force. Collapsing them into a single shuffled
// array would make turning shuffle back off mid-queue impossible — there'd be
// nothing left that remembered where the list really started — and
// re-shuffling on each toggle made the queue a different list every press.

import { api } from "../core.js";

export const QUEUE_CHANGED = "spotea:queuechange";

const QUEUE_KEY = "spotea-queue";
// Shuffle and repeat are kept apart from the queue, in localStorage rather
// than sessionStorage, because they aren't part of any one queue: they are
// how this listener wants their music played, and that doesn't stop being
// true when the app is closed. Stored with the queue they were reset to off
// on every fresh launch, and the panel came back offering an order the user
// had already said they didn't want.
const PREFS_KEY = "spotea-play-prefs";

// Survives the reload that resume.js forces on every bfcache restore (which
// on an iOS PWA is every trip to the home screen and back). Without this the
// queue would silently evaporate mid-listen and the track that was playing
// would simply be the last one — same failure the resume record exists to
// prevent, one level up.
// `repeat` is "off" | "all" | "one". Like shuffle it is a standing
// preference rather than part of a particular queue, so it survives
// clearQueue() and outlives whatever is playing.
// `shuffleOrder` is this queue's one shuffle of `ids`, decided when the queue
// is built and kept for as long as it lasts. `order` is whichever of the two
// is in force right now — see applyOrder.
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
  // Shape-checked rather than trusted: a record written by an older version
  // of this module (or a half-written one) would otherwise turn every
  // nextId() into a crash rather than a missing queue.
  if (!saved || !Array.isArray(saved.ids) || !Array.isArray(saved.order)) return;
  state = {
    source: saved.source ?? null,
    ids: saved.ids,
    // A record written before the shuffle order was kept has none. Building a
    // fresh one is the only option, and costs nothing: it can only differ
    // from the order that record was playing in if shuffle was on, and this
    // runs once per page load.
    shuffleOrder: Array.isArray(saved.shuffleOrder) ? saved.shuffleOrder : shuffled(saved.ids),
    order: saved.order,
    position: Number.isInteger(saved.position) ? saved.position : -1,
    shuffle: saved.shuffle === true,
    // Same shape check as the rest: a record from before repeat existed has
    // no such field, and an unknown value would otherwise make peekIndex
    // wrap on a mode nothing understands.
    repeat: REPEAT_MODES.includes(saved.repeat) ? saved.repeat : "off",
  };
}

/**
 * The last word on shuffle and repeat, run after restore() so it overrides
 * the copy that came in with the queue.
 *
 * Only when there is a record to read: with localStorage blocked and
 * sessionStorage working, the queue's own copy is still the best answer
 * available, and clobbering it with a default would be a downgrade.
 */
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
// The two records agree whenever they were written together, which is every
// time this tab wrote them. They can disagree across tabs: shuffle is shared
// (localStorage) and the queue is not (sessionStorage), so a second tab can
// come back holding a sequential order under a preference that has since been
// switched on elsewhere. Rebuilt only in that case — doing it unconditionally
// would reshuffle what's up next on every reload, and resume.js forces one
// each time an iOS PWA comes back from the home screen.
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

/**
 * Points `order` at one of the queue's two fixed orderings and puts the
 * pointer back on `keepId`, so nothing restarts.
 *
 * A list has exactly two orders and both are decided once, when the queue is
 * built: its own, and one shuffle of it. Turning shuffle on used to shuffle
 * again from scratch every time, which meant the queue panel was a different
 * list after every press and there was no way back to the order you had just
 * been looking at. Switching between two stable orders is also what lets the
 * panel keep its rows and its scroll position across a toggle.
 *
 * The cost is that there's no way to ask for a *different* random order. A
 * fresh shuffle comes with the next queue, which is often enough.
 */
function applyOrder(keepId) {
  state.order = (state.shuffle ? state.shuffleOrder : state.ids).slice();
  state.position = keepId == null ? -1 : state.order.indexOf(keepId);
}

function queueUrl(source) {
  // A hand-made list is addressed by id rather than by kind: the pinned three
  // are a fixed vocabulary that *is* the path, these are rows, and their order
  // is stored rather than derived from a filter (see routers/content.py's
  // user_playlist_queue).
  if (source.kind === "user-playlist") return `/content/queue/user-playlist/${source.id}`;
  return `/content/queue/playlist/${source.kind}`;
}

/** The playlist the loaded queue came from, or null. */
export function queueSource() {
  return state.source;
}

/**
 * The index `offset` steps from the current one, or null when there is
 * nothing there.
 *
 * Under repeat "all" the ends join up, so the last track's next is the first
 * one and the first track's previous is the last. That single rule is what
 * makes both the transport buttons and auto-advance wrap, without either of
 * them knowing about repeat at all.
 */
function peekIndex(offset) {
  if (state.position < 0 || !state.order.length) return null;
  const index = state.position + offset;
  if (index >= 0 && index < state.order.length) return index;
  if (state.repeat !== "all") return null;
  // A queue of one has no other end to join up with. Without this the modulo
  // below wrapped it onto itself — `1 % 1` is 0 — so turning repeat on with a
  // single track open lit both skip buttons, and pressing either one
  // restarted the track that was already playing. "Repeat the queue" on a
  // one-track queue is what repeat "one" already means.
  if (state.order.length < 2) return null;
  return ((index % state.order.length) + state.order.length) % state.order.length;
}

/** The id `offset` steps from the current one, without moving the pointer. */
function peek(offset) {
  const index = peekIndex(offset);
  return index === null ? null : state.order[index];
}

/** Read-only lookahead — drives both the transport's disabled state and the
 *  one-track-ahead download prefetch, neither of which may advance playback. */
export function peekNextId() {
  return peek(1);
}

export function peekPreviousId() {
  return peek(-1);
}

export function isShuffled() {
  return state.shuffle;
}

/** "off" | "all" | "one". */
export function repeatMode() {
  return state.repeat;
}

/**
 * The whole queue in play order — the Queue panel's input.
 *
 * All of it, including what's already been played. The panel used to be
 * handed only the tracks from the pointer onwards, which meant choosing the
 * tenth row rebuilt the list without the nine above it: the rows moved under
 * the finger that had just picked one, and there was no way back to a track
 * you'd passed. With the full order the panel is a fixed list and playing
 * something only moves a marker down it.
 */
export function queueOrder() {
  return state.order.slice();
}

/** What's playing, or null. The panel marks this row rather than re-rendering. */
export function currentId() {
  return state.position < 0 ? null : (state.order[state.position] ?? null);
}

/**
 * Advances repeat one step: off -> all -> one -> off.
 *
 * "one" deliberately doesn't change what the Next button does — pressing
 * Next means "play the next track", whatever repeat says. It only decides
 * what happens when a track ends on its own (see home/overlay.js).
 */
export function cycleRepeat() {
  const next = (REPEAT_MODES.indexOf(state.repeat) + 1) % REPEAT_MODES.length;
  state.repeat = REPEAT_MODES[next];
  announce();
  return state.repeat;
}

function step(offset) {
  // The index rather than `position + offset`: under repeat "all" the step
  // that runs off the end lands back at the other one.
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
 * Keeps the pointer honest about what's actually playing.
 *
 * Called for every track the player opens, however it was opened. A track
 * inside the queue just moves the pointer (this is what makes clicking row
 * 12 of a playlist continue from row 13). A track from somewhere else — a
 * Home shelf, an Explore result, a resumed session — means the queue no
 * longer describes what's playing, so it's dropped rather than left to
 * advance into an unrelated list the next time a track ends.
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

/**
 * Turns shuffle on or off. A standing preference, not an action: it holds
 * with no queue loaded and decides the order the next loadQueue() builds,
 * which is what lets the detail panel's shuffle button mean the same thing
 * before and during playback.
 */
export function toggleShuffle() {
  state.shuffle = !state.shuffle;
  if (state.order.length) applyOrder(state.order[state.position] ?? null);
  announce();
  return state.shuffle;
}

/**
 * Loads one channel's or playlist's full track order as the queue.
 *
 * `startId` is the track playback is already on (a track row was clicked and
 * the player is loading it right now) — it becomes the queue's current
 * position rather than restarting from the top, and under shuffle it stays
 * first so the click isn't overridden by the reshuffle.
 *
 * Returns the id to start playing, or null if the request failed or the
 * source turned out to be empty.
 */
export async function loadQueue(source, { startId = null } = {}) {
  const { ok, data } = await api(queueUrl(source), { errorMessage: "Could not load the queue" });
  if (!ok || !data?.ids?.length) return null;
  return setQueue(source, data.ids, { startId });
}

/**
 * The same thing with the ids already in hand.
 *
 * Explore's remote channel/playlist pages have no /content/queue/... endpoint
 * to ask: their rows only become Content rows at the moment playback starts,
 * and the request that creates them hands back the ids in list order (see
 * home/remote.js). Everything after that is an ordinary queue.
 */
export function setQueue(source, ids, { startId = null } = {}) {
  if (!ids?.length) return null;

  state.source = { kind: source.kind, id: source.id ?? null };
  state.ids = ids;
  // Number(): startId reaches here as a string from dataset reads, and the
  // ids are JSON numbers — indexOf across the two would never match.
  const start = startId == null ? null : Number(startId);
  const keep = state.ids.includes(start) ? start : null;
  // This queue's one shuffle, fixed here for as long as it lasts (see
  // applyOrder). A clicked track goes to the front of it so that pressing
  // play on a row under shuffle still starts there and still has the whole
  // rest of the list ahead of it, rather than resuming from wherever that
  // track happened to land in the permutation.
  state.shuffleOrder = keep == null ? shuffled(state.ids) : [keep, ...shuffled(state.ids.filter((id) => id !== keep))];
  applyOrder(keep);
  if (state.position === -1) state.position = 0;
  announce();
  return state.order[state.position];
}
