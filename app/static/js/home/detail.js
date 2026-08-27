// The "detail" panel: a track list with a hero above it, drilled into in
// place — a panel swap, not a navigation. Replaces the old standalone
// channel.html/content_list.html pages; folding them in here (rather than
// leaving them as separate documents) is what keeps the player overlay and
// mini-bar alive while browsing one, since there's no longer a document
// boundary for that DOM to fall off of.
//
// Several things open it, and they differ only in where the rows come from:
//   {playlist kind}       one of the three pinned   (from Library)
//   user-playlist/{id}    a list the user made      (from Library)
//   yt-playlist/{id}      a YouTube playlist        (from Explore)
//   yt-artist/{id}        an artist's profile       (from Explore)
//   yt-artist-songs/{id}  that artist's whole list   (from the profile)
//   yt-release/{id}       one album or single        (from the profile)
//   yt-mood/{params}      a mood's playlists         (from Explore)
// All but the first are "remote": their rows have no Content row yet, so
// playing from them goes through home/remote.js, which materializes the list
// first. The artist profile and a mood's playlist shelf are the two bodies
// that aren't a track list at all (see templates/_artist_panel.html and
// _mood_panel.html); their cards are wired below.

import { unfollowArtist } from "../content-actions.js";
import { classifyHash, noteConnection, showToast } from "../core.js";
import { refreshFragments, swapFragmentHtml } from "../fragments.js";
import { deviceTrackIds, renderDownloadsPanel } from "./device.js";
import { OPEN_ARTIST, openPlayer } from "./overlay.js";
import { PLAYLIST_CHANGED, PLAYLIST_DELETED } from "./playlists.js";
import { QUEUE_CHANGED, isShuffled, loadQueue, queueSource, setQueue, toggleShuffle } from "./queue.js";
import { wireScrollers } from "./scrollers.js";
import { ARTIST_FOLLOWED, followArtist, playRemoteList, playRemoteVideo } from "./remote.js";
import { activate } from "./tabs.js";

// Detail kinds whose rows come from YouTube rather than the database.
const REMOTE_KINDS = ["yt-playlist", "yt-artist", "yt-artist-songs", "yt-release", "yt-mood"];
const isRemoteKind = (kind) => REMOTE_KINDS.includes(kind);

// The one kind that is neither: "downloads" is drawn from IndexedDB by
// home/device.js, with no request of any sort. It routes exactly like a
// pinned playlist (its kind is its whole identity, see core.js's
// PLAYLIST_KINDS) and only the three places that would otherwise reach for
// the network — the fragment fetch, Play all, and a row click's queue —
// have to know the difference.
const isDeviceKind = (kind) => kind === "downloads";

// Remote fragment HTML already fetched this session, keyed by the exact
// detailUrl() it came from (page included, so a different page is
// correctly a cache miss rather than stale content). Local kinds (channel,
// the four pinned playlists) always refetch instead — those are a cheap DB
// read to begin with, and other actions in this app (saving, favoriting)
// can change what one shows. A remote kind costs a live YouTube Music
// read (see routers/partials.py) and nothing in this app mutates a listing
// it reads, so re-opening one already visited this session has nothing to
// gain from asking again. Capped so a long browsing session doesn't grow
// this forever; Map preserves insertion order, so the oldest entry is
// whatever .keys().next() gives back.
const REMOTE_CACHE_LIMIT = 20;
const remoteFragmentCache = new Map();

// Which tab this view belongs to. Drives both the no-history fallback in
// closeDetail and, via a data attribute on <html>, which tab button stays
// visually selected while the panel is open (see style.css) — the detail
// panel has no .tab-btn of its own.
const detailHome = (kind) => (isRemoteKind(kind) ? "explore" : "library");

// What's currently open, so a pagination click knows what to re-fetch
// without re-parsing the URL, and so "Back to Library" knows whether there's
// actually a Library entry behind this one to pop back to (pushed) or
// whether this view was reached without one — a deep link, a fresh tab, a
// reload — in which case history.back() would leave the app entirely rather
// than showing Library. { kind, id, page, pushed } | null.
let current = null;

// Kinds that carry an id put it in the path; the pinned playlists are the
// kind itself, and take the /playlist/{kind} route. A hand-made playlist is
// the one local kind with an id — /partials/detail/user-playlist/{id}.
const hasId = (kind) => isRemoteKind(kind) || kind === "user-playlist";

function detailUrl(kind, id, page, title) {
  const base = hasId(kind) ? `/partials/detail/${kind}/${id}` : `/partials/detail/playlist/${kind}`;
  const params = new URLSearchParams();
  if (page > 1) params.set("page", page);
  // Only yt-mood takes this (see routers/partials.py's remote_mood_fragment)
  // — passed straight through rather than threaded into hashFor/classifyHash
  // too, since it's a display optimization, not part of what a mood route
  // means: a reload or a shared link works fine without it, just at the cost
  // of one extra lookup server-side.
  if (title) params.set("title", title);
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function hashFor(kind, id, page) {
  const path = hasId(kind) ? `${kind}/${id}` : kind;
  return page > 1 ? `#${path}?page=${page}` : `#${path}`;
}

function showLoading() {
  const panel = document.getElementById("detail-panel");
  if (panel) {
    panel.innerHTML = `<div class="detail-loading"><span class="spinner spinner-lg" role="status" aria-label="Loading"></span></div>`;
  }
}

/** Oldest-out, so a long browse doesn't grow this without bound. */
function cacheRemoteFragment(url, html) {
  remoteFragmentCache.set(url, html);
  if (remoteFragmentCache.size > REMOTE_CACHE_LIMIT) {
    remoteFragmentCache.delete(remoteFragmentCache.keys().next().value);
  }
}

/** resolveRelease's "the request failed and it already said so" answer,
 *  distinct from null ("this is a normal multi-track release"). */
const FAILED = Symbol("release-failed");

/** Remembers which releases turned out to be one track, so a second click on
 *  the same single doesn't re-ask YouTube. The multi-track answer is cached
 *  too, as HTML, in remoteFragmentCache below — same request, either way. */
const singleReleaseCache = new Map();

/**
 * Asks what a release is, once.
 *
 * Returns the track to play for a one-track release, null for anything
 * longer (having primed remoteFragmentCache with its panel, so the
 * openDetail call right after this costs no second request), or FAILED if
 * the fetch didn't work and the user has already been told.
 */
async function resolveRelease(browseId) {
  const url = detailUrl("yt-release", browseId, 1);
  if (singleReleaseCache.has(url)) return singleReleaseCache.get(url);
  if (remoteFragmentCache.has(url)) return null;

  let res;
  try {
    res = await fetch(url);
  } catch (err) {
    // The request never arrived, which says something about the connection
    // rather than about this release — and this module's fetches are not
    // api() calls, so nothing else here would tell the banner.
    noteConnection(false);
    showToast("Could not load this page");
    return FAILED;
  }
  noteConnection(true);
  if (!res.ok) {
    showToast(res.status === 404 ? "That's gone." : "Could not load this page");
    return FAILED;
  }

  if (res.headers.get("content-type")?.includes("application/json")) {
    const track = await res.json();
    singleReleaseCache.set(url, track);
    return track;
  }
  cacheRemoteFragment(url, await res.text());
  return null;
}

/**
 * Opens one of the four sources listed at the top of this file. `id` is a
 * a YouTube id for the remote kinds, and null for
 * a pinned playlist (whose kind is its whole identity).
 *
 * `replace` is for callers syncing to a URL that's already current (initial
 * boot, popstate, a pagination click within the same detail view) — a fresh
 * "the user just clicked into this" open pushes a new history entry instead,
 * so the back button can return where it came from.
 *
 */
export async function openDetail(kind, id, { page = 1, replace = false, title } = {}) {
  // Every kind but one is a request, and offline a request is a spinner that
  // resolves into a toast. The tiles that lead here are already out of reach
  // (see style.css's body.is-offline), so what is left to catch is a deep
  // link, a reload on an old hash, or the back button walking into one.
  if (!isDeviceKind(kind) && document.body.classList.contains("is-offline")) {
    showToast("You're offline — only your Downloads are available");
    activate("library");
    return;
  }

  // A one-track release plays instead of opening (see routers/partials.py's
  // remote_release_fragment). Resolved here rather than at the click site so
  // that every way into a release goes through it — the card, a reload on a
  // #yt-release/... hash left over from before this changed, the back button
  // — and resolved before the history push below, because a single must not
  // leave a panel entry behind to go "back" to.
  if (kind === "yt-release") {
    const single = await resolveRelease(id);
    if (single === FAILED) return;
    if (single) {
      await playRemoteVideo(single);
      return;
    }
  }

  // A pagination click within the view that's already open keeps whatever
  // "is there a Library entry behind this" answer the original open
  // established — pagination itself always replaces, so it must not flip a
  // real push back to false.
  const isSameTarget = current && current.kind === kind && String(current.id) === String(id);
  const pushed = isSameTarget ? current.pushed : !replace;
  current = { kind, id, page, pushed };
  document.documentElement.dataset.detailHome = detailHome(kind);
  activate("detail", { updateHistory: false });
  const hash = hashFor(kind, id, page);
  if (replace) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);

  if (isDeviceKind(kind)) {
    await renderDownloadsPanel();
    afterPanelSwap();
    return;
  }

  const url = detailUrl(kind, id, page, title);
  const cached = isRemoteKind(kind) ? remoteFragmentCache.get(url) : undefined;
  if (cached !== undefined) {
    swapFragmentHtml(cached);
    afterPanelSwap();
    return;
  }

  showLoading();

  let res;
  try {
    res = await fetch(url);
  } catch (err) {
    // See resolveRelease above: this is often the very first thing to notice
    // a connection has gone, since opening a list is what people do first.
    noteConnection(false);
    showToast("Could not load this page");
    return;
  }
  noteConnection(true);
  if (!res.ok) {
    showToast(res.status === 404 ? "That's gone." : "Could not load this page");
    return;
  }
  const html = await res.text();
  if (isRemoteKind(kind)) cacheRemoteFragment(url, html);
  swapFragmentHtml(html);
  afterPanelSwap();
}

/** Everything the freshly-swapped panel markup needs wiring up.
 *
 *  Both swap paths (cache hit and fetch) go through here so neither can
 *  drift from the other — the cached branch used to only sync the shuffle
 *  button, which was already one thing too many to keep in two places.
 */
function afterPanelSwap() {
  // The swap brings in a brand-new shuffle button, which knows nothing about
  // a preference set on some other view or in the player.
  syncShuffleButton();
  // An artist profile arrives with shelves in it. Nothing else this panel
  // renders has a horizontal row, so this is a no-op for every other kind.
  wireScrollers();
}

// What the panel currently shows, in the shape queue.js takes: the channel
// or playlist "Play all" would fill the queue from.
function currentSource() {
  return current && { kind: current.kind, id: current.id ?? null };
}

function isSameSource(a, b) {
  return Boolean(a && b && a.kind === b.kind && String(a.id) === String(b.id));
}

function syncShuffleButton() {
  const btn = document.getElementById("detail-shuffle");
  if (!btn) return;
  btn.classList.toggle("is-on", isShuffled());
  btn.setAttribute("aria-pressed", String(isShuffled()));
}

/**
 * "Play all": fills the queue from the whole channel/playlist — every page of
 * it, not the twenty rows on screen — and starts on whichever track that
 * order puts first, which is a random one while shuffle is on.
 *
 * A remote list has no server-side queue to fetch and no second page, so
 * home/remote.js builds it from the rows on screen instead; either way the
 * queue that comes out is the same thing.
 */
async function playAll(button) {
  const source = currentSource();
  if (!source) return;
  if (isRemoteKind(source.kind)) {
    playRemoteList(source, { button });
    return;
  }
  // No /content/queue round trip for the device's own list: the panel was
  // built from what is on this phone, so the ids are already here — and on
  // the screen this exists for, the request would not arrive anyway.
  if (isDeviceKind(source.kind)) {
    const ids = deviceTrackIds();
    if (!ids.length) {
      showToast("Nothing to play here");
      return;
    }
    const startId = setQueue(source, ids);
    openPlayer(startId ?? ids[0]);
    return;
  }
  // The fetch is a round trip and the button is the kind people press twice;
  // without this the second press would build a second queue and jump
  // playback back to its start.
  button.disabled = true;
  try {
    const startId = await loadQueue(source);
    if (startId == null) {
      showToast("Nothing to play here");
      return;
    }
    openPlayer(startId);
  } finally {
    button.disabled = false;
  }
}

// The in-panel "Back to Library" control ends up here (the browser's own
// back button doesn't — that's the popstate listener below). Prefers
// history.back() so forward still works afterward, same as any other real
// navigation, but falls back to switching to Library in place when this view
// was never pushed (see `current.pushed` above) — otherwise back() would pop
// past the app entirely.
function closeDetail() {
  const pushed = current?.pushed;
  const home = detailHome(current?.kind);
  current = null;
  if (pushed) history.back();
  else activate(home);
}

async function handleUnfollow(artistId, button) {
  button.disabled = true;
  const ok = await unfollowArtist(artistId);
  if (!ok) {
    button.disabled = false;
    return;
  }
  // This detail view no longer means anything — back to Library, whose grid
  // needs to lose the tile too.
  closeDetail();
  refreshFragments();
}

function pageFromHref(href) {
  const match = href.match(/[?&]page=(\d+)/);
  return match ? Number(match[1]) : 1;
}

/** Opens a release card, wherever it was rendered — an artist's profile or
 *  Home's "New releases" shelf.
 *
 *  Marked busy for the duration, because what this click does isn't decided
 *  until the answer arrives: a multi-track release swaps the panel in
 *  (visible straight away), but a single plays and leaves the page where it
 *  was, and without this the tap would look ignored. */
function openReleaseCard(card) {
  card.classList.add("is-loading");
  openDetail("yt-release", card.dataset.releaseId).finally(() => {
    card.classList.remove("is-loading");
  });
}

export function setupDetailPanel() {
  const panel = document.getElementById("detail-panel");
  if (!panel) return;

  // Home's "New releases" shelf holds the same cards the artist profile
  // does, so they need the same handler — and #detail-panel's listener below
  // can't see them. Bound here rather than in home/overlay.js because that
  // module can't import this one (it is imported *by* it), and rather than
  // in home/library.js because opening a release is this module's job.
  //
  // No conflict with overlay.js's own Home click handler: that one requires
  // an <a> to have been clicked, and a release card's only control is a
  // <button>.
  document.getElementById("tab-home")?.addEventListener("click", (event) => {
    const releaseCard = event.target.closest(".release-card");
    if (releaseCard) openReleaseCard(releaseCard);
  });

  // Delegated, not bound to specific ids: swapFragmentHtml replaces
  // #detail-panel's children wholesale on every open/paginate, which would
  // take a direct listener with it.
  panel.addEventListener("click", (event) => {
    if (event.target.closest("#detail-back-btn")) {
      closeDetail();
      return;
    }

    const unfollowBtn = event.target.closest("#unfollow-artist-btn");
    if (unfollowBtn) {
      handleUnfollow(unfollowBtn.dataset.artistId, unfollowBtn);
      return;
    }

    const followBtn = event.target.closest("#follow-artist-btn");
    if (followBtn) {
      followArtist(followBtn.dataset.channelUrl, followBtn);
      return;
    }

    const playAllBtn = event.target.closest("#detail-play-all");
    if (playAllBtn) {
      playAll(playAllBtn);
      return;
    }

    // Shuffle is a toggle, not a second play button: it reorders a queue
    // already running through this view in place (keeping the current track
    // playing) and otherwise just decides the order the next Play builds. So
    // pressing it while something plays doesn't interrupt anything, and
    // pressing it on a view nobody is listening to doesn't start anything.
    if (event.target.closest("#detail-shuffle")) {
      toggleShuffle();
      return;
    }

    // The artist profile's own controls. Checked before the track rows
    // below because a profile has both: shelves of cards and a preview list.
    const bioToggle = event.target.closest("#artist-bio-toggle");
    if (bioToggle) {
      const expanded = bioToggle.getAttribute("aria-expanded") === "true";
      bioToggle.setAttribute("aria-expanded", String(!expanded));
      bioToggle.textContent = expanded ? "More" : "Less";
      document.getElementById("artist-bio")?.classList.toggle("is-expanded", !expanded);
      return;
    }

    const releaseCard = event.target.closest(".release-card");
    if (releaseCard) {
      openReleaseCard(releaseCard);
      return;
    }

    // A "Similar artists" card. Same target as a channel card in Explore:
    // the id is a browse id, and the artist route works out what it is.
    const artistCard = event.target.closest(".shelf-channel-card");
    if (artistCard) {
      openDetail("yt-artist", artistCard.dataset.channelId);
      return;
    }

    // The Videos shelf. A card rather than a row because these carry no
    // duration (see music.ArtistProfile.videos), so it plays like one of
    // Explore's song cards rather than going through the row handler.
    const videoCard = event.target.closest(".rec-card[data-video-id]");
    if (videoCard) {
      playRemoteVideo(videoCard.dataset, videoCard.querySelector(".rec-play"));
      return;
    }

    // A mood panel's playlist cards (_mood_panel.html) — same target shape
    // Explore's own chart/mood shelves use for the same reason (see
    // explore.js's body click handler), just wired here instead since these
    // live inside the detail panel, not Explore's browse panel.
    const playlistCard = event.target.closest(".rec-card[data-playlist-id]");
    if (playlistCard) {
      openDetail("yt-playlist", playlistCard.dataset.playlistId);
      return;
    }

    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    // "All 150 songs". A real link so it survives with JS disabled and
    // reads as one, but handled here for the same reason pagination is:
    // a hash change alone wouldn't swap the panel.
    const seeAll = event.target.closest(".artist-see-all");
    if (seeAll && current) {
      event.preventDefault();
      openDetail("yt-artist-songs", current.id);
      return;
    }

    const pageLink = event.target.closest(".pagination-btn:not(.is-disabled)");
    if (pageLink && current) {
      event.preventDefault();
      openDetail(current.kind, current.id, { page: pageFromHref(pageLink.getAttribute("href")), replace: true });
      return;
    }

    const trackLink = event.target.closest(".track-row .track-link");
    if (trackLink) {
      event.preventDefault();
      const row = trackLink.closest(".track-row");
      const source = currentSource();

      // A remote row has no content id yet — clicking it means the same
      // thing ("start here and keep going"), it just has to materialize the
      // list before there's a queue to do it with.
      if (row.dataset.videoId) {
        if (source) playRemoteList(source, { startVideoId: row.dataset.videoId });
        return;
      }

      const contentId = row.dataset.contentId;
      // Clicking a row means "start here and keep going", the same as it does
      // in any music app — so the rest of this channel/playlist becomes the
      // queue, not just this one track. Checked before openPlayer, which
      // clears a queue the clicked track isn't part of (see queue.js's
      // noteCurrent) and would otherwise make this always look like a miss.
      const alreadyQueued = isSameSource(queueSource(), source);
      // Not awaited: the queue is only needed when this track ends, minutes
      // from now, and holding playback for a request that has nothing to do
      // with it would put a round trip in front of every single play.
      openPlayer(contentId);
      if (alreadyQueued || !source) return;
      // Same meaning, no request — see playAll above.
      if (isDeviceKind(source.kind)) setQueue(source, deviceTrackIds(), { startId: contentId });
      else loadQueue(source, { startId: contentId });
    }
  });

  // The player has its own shuffle toggle, and both drive the same
  // preference — whichever one is pressed, this panel's button has to follow.
  document.addEventListener(QUEUE_CHANGED, syncShuffleButton);

  // A track was taken out of the open playlist. The row, the hero's count and
  // the pagination all come from the server, so the panel is re-opened rather
  // than having the row plucked out of the DOM — the same reasoning that made
  // every other list here a fragment. `replace: true` so the re-render
  // doesn't add a history entry the back button would have to walk through.
  document.addEventListener(PLAYLIST_CHANGED, (event) => {
    if (current?.kind !== "user-playlist") return;
    if (String(current.id) !== String(event.detail?.playlistId)) return;
    openDetail(current.kind, current.id, { page: current.page, replace: true });
  });

  // And the whole list is gone, so there is no panel left to show.
  document.addEventListener(PLAYLIST_DELETED, (event) => {
    if (current?.kind !== "user-playlist") return;
    if (String(current.id) !== String(event.detail?.playlistId)) return;
    closeDetail();
  });

  // An artist just finished being followed — from an Explore search result, a
  // recommendation card, or the Follow button on their own page.
  document.addEventListener(ARTIST_FOLLOWED, (event) => {
    // A cached artist view's Follow button would otherwise still say
    // "Follow" if reopened later — the event carries the new row's id, not
    // the browse id its cache entry is keyed on, so dropping the whole cache
    // is simpler than tracking that mapping for an action this infrequent.
    remoteFragmentCache.clear();
    // Deliberately does not open their page. Following used to jump straight
    // to the profile, which meant a search result's Follow button and the
    // result row itself did the same thing — and the one you press when you
    // already know who you're adding is precisely the one you don't want
    // taken anywhere. The row still opens the profile; this only follows.
    // The button says "Following" either way (see home/remote.js), so the
    // press is still acknowledged where it happened.
  });

  // The player's artist line was tapped. It has already collapsed itself to
  // the mini bar; all that's left is the page it asked for.
  document.addEventListener(OPEN_ARTIST, (event) => {
    openDetail("yt-artist", event.detail.pageId);
  });

  window.addEventListener("popstate", () => {
    const info = classifyHash(location.hash.slice(1));
    if (info.type === "detail") openDetail(info.kind, info.id, { page: info.page, replace: true });
    else if (info.type === "player") openPlayer(info.id);
    else current = null;
  });
}

// Resolves whatever hash the page loaded with — called once at boot, after
// the pre-paint script has already gotten first paint right (see
// index.html) and setupPlayerOverlay/resumeOverlayIfNeeded are ready to
// receive an openPlayer call.
export function handleInitialRoute() {
  const info = classifyHash(location.hash.slice(1));
  if (info.type === "detail") openDetail(info.kind, info.id, { page: info.page, replace: true });
  else if (info.type === "player") openPlayer(info.id);
}
