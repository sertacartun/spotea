// Explore: one search box over both songs and channels, and the
// browse shelves under it — interest-based ones when there are interests,
// charts and moods to browse either way (see services/recommendations.py).
//
// What happens when you act on a result lives in home/remote.js (playing a
// song, following a channel) or in the detail panel (opening a playlist, an
// artist, or a mood's playlists drills into home/detail.js's yt-playlist/
// yt-artist/yt-mood kinds, rather than Explore growing a track list of its
// own).
//
// The tab shows one of two regions at a time: the browse/recommendations
// panel by default, and the search results while there's a query.

import { api, debounce, escapeHtml, formatDuration, setupSearchClear } from "../core.js";
import { openDetail } from "./detail.js";
import { wireScrollers } from "./scrollers.js";
import { followArtist, playRemoteVideo } from "./remote.js";
import { onTabActivated } from "./tabs.js";

// "monthly listeners", not "subscribers": the number comes from YouTube's
// subscriber_count field, but Library already labels the same figure the way a
// music app does (see _library_grid.html), and one number can't have two names
// in one app.
function formatListeners(count) {
  if (count == null) return "";
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M monthly listeners`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}K monthly listeners`;
  return `${count} monthly listeners`;
}

export function renderChannelResults(results, containerId = "channel-search-results") {
  const list = document.getElementById(containerId);
  if (!list) return;

  if (!results.length) {
    list.innerHTML = `<li class="search-empty">No artists found</li>`;
    return;
  }

  list.innerHTML = results
    .map((r) => {
      const thumb = r.thumbnail_url
        ? `<img class="search-result-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" loading="lazy" />`
        : `<span class="search-result-thumb"></span>`;
      const subs =
        r.subscriber_count != null
          ? `<span class="search-result-subs">${formatListeners(r.subscriber_count)}</span>`
          : "";
      // Two targets on one row: the row itself previews what the channel
      // has (an artist's tracks where there are any, its latest uploads
      // otherwise — see the click handler), the button follows it outright
      // for when you already know what you're adding.
      return `
        <li
          class="search-result search-result-channel"
          data-channel-id="${escapeHtml(r.channel_id)}"
          data-thumbnail-url="${escapeHtml(r.thumbnail_url || "")}"
        >
          ${thumb}
          <div class="search-result-info">
            <span class="search-result-title">${escapeHtml(r.title)}</span>
            ${subs}
          </div>
          <button type="button" class="btn-add-channel" data-channel-url="${escapeHtml(r.channel_url)}">Follow</button>
        </li>
      `;
    })
    .join("");
}

/** A song's artist, as a way into their page when the result says which
 *  channel it belongs to.
 *
 *  Plain text when it doesn't. The fallback yt-dlp search (see
 *  routers/explore.py's search_video_feeds) doesn't reliably report a
 *  channel on a flat search entry, and a link that opens nothing is worse
 *  than no link. The id itself is the artist's "Topic" channel — the
 *  yt-artist panel is what turns that into the real artist, and falls back
 *  to the plain channel view for an id that isn't one at all (see
 *  services/remote_detail.py). */
function artistNameHtml(item) {
  const name = escapeHtml(item.channel_title || "");
  if (!name || !item.channel_id) return name;
  return `<button type="button" class="artist-link" data-channel-id="${escapeHtml(item.channel_id)}">${name}</button>`;
}

// Rows, not a whole list, so the caller owns the empty state.
function videoRowsHtml(results) {
  return results
    .map((r) => {
      const thumb = r.thumbnail_url
        ? `<img class="video-search-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" />`
        : `<span class="video-search-thumb"></span>`;
      const duration = r.duration_seconds != null ? formatDuration(r.duration_seconds) : "";
      const meta = [artistNameHtml(r), duration].filter(Boolean).join(" · ");
      return `
        <li
          class="search-result video-search-result"
          data-video-id="${escapeHtml(r.video_id)}"
          data-title="${escapeHtml(r.title)}"
          data-thumbnail-url="${escapeHtml(r.thumbnail_url || "")}"
          data-duration-seconds="${r.duration_seconds ?? ""}"
          data-channel-title="${escapeHtml(r.channel_title || "")}"
          data-artist-credit="${escapeHtml(r.artist_credit || "")}"
          data-channel-id="${escapeHtml(r.channel_id || "")}"
        >
          ${thumb}
          <div class="search-result-info">
            <span class="search-result-title">${escapeHtml(r.title)}</span>
            <span class="search-result-subs">${meta}</span>
          </div>
          <button type="button" class="btn-icon video-search-play" aria-label="Play">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><use href="#i-play" /></svg>
          </button>
        </li>
      `;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// "For you": interest-based recommendations
// ---------------------------------------------------------------------------

// Every card here is a thing the user doesn't have yet, so none of them carry
// a content id, a save button or a download badge — they reuse .card/.thumb/
// .card-body purely for the shelf geometry Home already defines.

function recVideoCardHtml(video) {
  const thumb = video.thumbnail_url
    ? `<img src="${escapeHtml(video.thumbnail_url)}" alt="" loading="lazy" />`
    : "";
  // No duration over the artwork — that's a video-thumbnail convention, and
  // _content_card.html dropped it for the same reason. Duration belongs in a
  // track list, on the right.
  return `
    <article
      class="card rec-card"
      data-video-id="${escapeHtml(video.video_id)}"
      data-title="${escapeHtml(video.title)}"
      data-thumbnail-url="${escapeHtml(video.thumbnail_url || "")}"
      data-duration-seconds="${video.duration_seconds ?? ""}"
      data-channel-title="${escapeHtml(video.channel_title || "")}"
      data-artist-credit="${escapeHtml(video.artist_credit || "")}"
      data-channel-id="${escapeHtml(video.channel_id || "")}"
    >
      <button type="button" class="thumb rec-play" aria-label="Play ${escapeHtml(video.title)}">
        ${thumb}
      </button>
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(video.title)}">${escapeHtml(video.title)}</h3>
        <p class="card-channel">${artistNameHtml(video)}</p>
      </div>
    </article>
  `;
}

/** A charting artist. Same data shape as a channel card, and opens the same
 *  way; what it deliberately lacks is the Add button. The id on a chart
 *  entry is the artist's auto-generated "Topic" channel rather than the one
 *  they upload to, so following straight from this card would follow the
 *  wrong thing. Follow lives on the artist page, which knows both ids. */
function artistCardHtml(artist) {
  const avatar = artist.thumbnail_url
    ? `<img class="shelf-channel-avatar" src="${escapeHtml(artist.thumbnail_url)}" alt="" loading="lazy" />`
    : `<span class="shelf-channel-avatar"></span>`;
  return `
    <article
      class="card shelf-channel-card"
      data-channel-id="${escapeHtml(artist.channel_id)}"
    >
      ${avatar}
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(artist.title)}">${escapeHtml(artist.title)}</h3>
        <p class="card-date">${escapeHtml(formatListeners(artist.subscriber_count))}</p>
      </div>
    </article>
  `;
}

function recPlaylistCardHtml(playlist) {
  const thumb = playlist.thumbnail_url
    ? `<img src="${escapeHtml(playlist.thumbnail_url)}" alt="" loading="lazy" />`
    : "";
  return `
    <article
      class="card rec-card"
      data-playlist-id="${escapeHtml(playlist.playlist_id)}"
      data-title="${escapeHtml(playlist.title)}"
    >
      <button type="button" class="thumb" aria-label="Open ${escapeHtml(playlist.title)}">
        ${thumb}
      </button>
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(playlist.title)}">${escapeHtml(playlist.title)}</h3>
        <p class="card-channel">${escapeHtml(playlist.channel_title || "Playlist")}</p>
      </div>
    </article>
  `;
}

/** A whole shelf, or nothing at all when that kind came back empty — an
 *  empty "Playlists" heading is worse than no heading. */
function shelfHtml(title, items, cardHtml, lead = false) {
  if (!items.length) return "";
  return `
    <div class="shelf${lead ? " shelf--lead" : ""}">
      <div class="shelf-header"><h3 class="shelf-title">${escapeHtml(title)}</h3></div>
      <div class="shelf-row">${items.map(cardHtml).join("")}</div>
    </div>
  `;
}

function moodChipHtml(mood) {
  return `
    <button
      type="button"
      class="channel-chip"
      data-mood-params="${escapeHtml(mood.params)}"
      data-mood-title="${escapeHtml(mood.title)}"
    >
      <span>${escapeHtml(mood.title)}</span>
    </button>
  `;
}

/** Moods: a chip row rather than shelfHtml's card grid. A mood category has
 *  no artwork of its own — only the playlists inside it do — so a text chip
 *  (the same one Home's "Recently followed" row uses) reads better than an
 *  image card with nothing to show. Clicking one opens all of that mood's
 *  playlists (see templates/_mood_panel.html), not a track list, so the
 *  user picks a playlist from there rather than this app guessing which one
 *  they meant.
 *
 *  Called "Moods" and not "Moods & genres" because there are no genres in
 *  it: ytmusicapi fails to parse 25 of YouTube Music's 40 categories and
 *  they are every single entry under Genres (see music.MOOD_SECTION), so
 *  only the moods section is listed. The old name promised Rock and Jazz
 *  and then showed neither. If that parser is ever fixed the name comes
 *  back with them. */
function moodsShelfHtml(moods) {
  if (!moods.length) return "";
  return `
    <div class="shelf">
      <div class="shelf-header"><h3 class="shelf-title">Moods</h3></div>
      <div class="channel-row">${moods.map(moodChipHtml).join("")}</div>
    </div>
  `;
}

function renderRecommendations(data) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  const shelves = [
    // Moods first, and it is the only shelf that can't be empty: it needs
    // nothing followed and nothing typed, so on a library that has just
    // been created it is the difference between Explore being a place to
    // start and Explore being a blank page with a heading on it. The
    // shelves below it are all "here is more of what you already have",
    // which is exactly what a new library hasn't got yet.
    moodsShelfHtml(data.moods),
    // Songs and Artists used to both be typed-interest search, same as
    // Playlists still is — and both went badly for anything that wasn't
    // literally a song title or an artist's name, since an interest was
    // routinely a genre or mood instead. Both are now built from artists
    // actually followed rather than typed text (see
    // services/recommendations.py's _songs_from_followed and
    // _similar_to_followed) — empty for a library with nothing followed
    // yet, and deliberately not seeded with anything else in that case.
    // lead: the first content shelf here, sized larger so Explore is not five
    // identical rows stacked on each other.
    shelfHtml("Songs", data.videos, recVideoCardHtml, true),
    shelfHtml("Playlists", data.playlists, recPlaylistCardHtml),
    // Labelled "Artists you may like" rather than "Similar artists": the
    // shelf is a personal recommendation built from everyone followed, not
    // a "similar to X" list for one artist. The artist page's own shelf
    // keeps the "Similar artists" name, because there it really is one.
    // The `similar_artists` payload key is unchanged — this is copy only.
    shelfHtml("Artists you may like", data.similar_artists, artistCardHtml),
    // Then what everyone gets regardless: this week's charts.
    shelfHtml("Charts", data.charts, recPlaylistCardHtml),
    shelfHtml("Charting artists", data.chart_artists, artistCardHtml),
  ].join("");

  body.innerHTML =
    shelves ||
    `<p class="muted">Nothing came back this time. Try refreshing, or add a different interest in Settings.</p>`;
  // Shelves built here never pass through the fragment swap that normally
  // wires drag-scrolling, so they have to ask for it themselves.
  wireScrollers();
}

// The payload of the batch currently rendered, as JSON. Re-rendering shelves
// that came back identical replaces every card — and every <img> in them —
// with a fresh copy of itself, which is a visible flash of empty thumbnails
// in exchange for nothing. Identical is the normal case: the server only
// rebuilds a batch once the profile's chosen refresh interval has elapsed
// (see services/recommendations.py), so most re-checks answer with exactly
// what is already on screen.
let renderedPayload = null;

// The load currently in flight, so a second caller joins it instead of
// paying for its own round trip. The tab-activation check in particular
// fires while the boot fetch is still running on any slow first load.
let inFlight = null;

/**
 * Fetches the current batch and swaps the shelves in only if it differs from
 * what is already rendered.
 *
 * `placeholder` is the only thing that ever puts a loading line into the
 * panel, and only two callers pass it: the boot fetch (nothing is on screen
 * yet, and the panel is usually not even the visible tab) and the app-wide
 * Refresh. Opening Explore deliberately never does — the batch is fetched in
 * the background at boot and re-checked quietly afterwards, so the tab is
 * something you enter, not something you wait on.
 */
async function loadRecommendations({ force = false, placeholder = false } = {}) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;
  if (inFlight && !force) return inFlight;

  if (placeholder) {
    body.innerHTML = `<p class="search-loading"><span class="spinner"></span>Finding things you might like…</p>`;
  }

  const load = (async () => {
    const { ok, data } = await api(force ? "/recommendations/refresh" : "/recommendations", {
      method: force ? "POST" : "GET",
      errorMessage: "Could not load recommendations",
    });

    if (!ok) {
      // Only when this call is what put the placeholder there — a background
      // re-check that fails leaves the shelves it could not improve on, which
      // is the right outcome and needs no announcement.
      if (placeholder) {
        body.innerHTML = `<p class="muted">Couldn't reach YouTube for recommendations just now.</p>`;
      }
      return;
    }

    const payload = JSON.stringify(data);
    if (payload === renderedPayload) return;
    renderedPayload = payload;
    renderRecommendations(data);
  })();

  if (!force) inFlight = load;
  try {
    await load;
  } finally {
    if (inFlight === load) inFlight = null;
  }
}

/**
 * Re-fetches in the background, swapping the shelves in only if they changed.
 *
 * Called after the interest list is saved (see home/settings.js, which the
 * onboarding wizard's genre step also goes through) and by that wizard just
 * before it closes. Both are moments the cached batch became an answer to a
 * question nobody asked — it is keyed to the interest list, so a changed list
 * means the next read rebuilds it, and a rebuild is several live YouTube
 * searches. Doing it here means that cost is paid while the user is still
 * somewhere else, rather than by whoever opens Explore next and has to sit in
 * front of a spinner for it.
 */
export function reloadRecommendations() {
  return loadRecommendations();
}

/**
 * Rebuilds the shelves now, ignoring the cache. There is no dedicated button
 * for this in Explore: it's what the app-wide "Refresh feeds" control calls
 * (wired in pages/index.js), so one press means "go and look at everything
 * again" rather than the tab carrying a second, competing refresh of its own.
 */
export async function refreshRecommendations() {
  await loadRecommendations({ force: true, placeholder: true });
}

export function setupRecommendations() {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  // Fetched as soon as the app loads, not deferred until Explore is opened —
  // the shelves are ready to show the first time someone switches to the tab
  // instead of making that switch pay for the YouTube round trip. The
  // placeholder goes into a panel that is almost always the hidden tab; it is
  // there for the one case where it isn't (a #explore deep link on a cold
  // cache), so that tab has something to say for itself while the first batch
  // is being built.
  loadRecommendations({ placeholder: true });

  // Re-checked on every later switch to Explore too, so a profile that keeps
  // the tab around gets a fresh batch once its chosen interval elapses
  // without needing a reload. Never with a placeholder, and never re-rendering
  // an unchanged batch: entering the tab should show what's there, not blank
  // it out and rebuild it in front of the person who just arrived.
  onTabActivated((tab) => {
    if (tab === "explore") loadRecommendations();
  });

  body.addEventListener("click", (event) => {
    // Checked before anything else: the artist's name sits inside cards that
    // are themselves clickable, so the outer handler would otherwise swallow
    // it and start playing the song.
    const artistLink = event.target.closest(".artist-link");
    if (artistLink) {
      openDetail("yt-artist", artistLink.dataset.channelId);
      return;
    }

    // Checked before the card itself: a channel card is clickable as a whole
    // (preview it) but carries its own Add button (follow it outright).
    const addButton = event.target.closest(".btn-add-channel");
    if (addButton) {
      followArtist(addButton.dataset.channelUrl, addButton);
      return;
    }

    // Both channel-ish cards open the same route, and the server decides
    // what it is: an artist gets their track list, anything else falls back
    // to the channel's uploads (see remote_artist_context). The avatar hint
    // only matters on that fallback — an artist page brings its own
    // portrait — but the chart card has none to send anyway.
    const channelCard = event.target.closest(".shelf-channel-card");
    if (channelCard) {
      openDetail("yt-artist", channelCard.dataset.channelId);
      return;
    }

    // A "Moods & genres" chip — opens that mood's whole playlist shelf
    // (see templates/_mood_panel.html), not a track list. The title rides
    // along so the panel doesn't have to pay an extra lookup for it (see
    // routers/partials.py's yt-mood route).
    const moodChip = event.target.closest(".channel-chip[data-mood-params]");
    if (moodChip) {
      openDetail("yt-mood", moodChip.dataset.moodParams, { title: moodChip.dataset.moodTitle });
      return;
    }

    // Song and playlist cards are clickable anywhere, not just on the button
    // in their artwork — the button is there for keyboard and screen-reader
    // users, and which dataset the card carries says what it is.
    const card = event.target.closest(".rec-card");
    if (!card) return;
    if (card.dataset.videoId) playRemoteVideo(card.dataset, card.querySelector(".rec-play"));
    else if (card.dataset.playlistId) openDetail("yt-playlist", card.dataset.playlistId);
  });
}

// One of Explore's two regions is visible at a time: the browse panel
// (recommendations) by default, the search results while there's a query.
function showExplorePanel(visibleId) {
  for (const panelId of ["explore-browse-panel", "explore-results-panel"]) {
    const panel = document.getElementById(panelId);
    if (panel) panel.hidden = panelId !== visibleId;
  }
  // The tab strip lives in the sticky head above both panels rather than
  // inside the results one (see index.html — one sticky block, so the strip's
  // offset doesn't have to be hardcoded to the field's height), so it has to
  // be shown and hidden explicitly with them.
  const tabs = document.getElementById("explore-search-tabs");
  if (tabs) tabs.hidden = visibleId !== "explore-results-panel";
}

// The two halves of a search result, one at a time. Both are still fetched
// together (see runSearch) — the tabs decide what is on screen, not what is
// asked for, so switching between them costs nothing and never waits.
const SEARCH_TABS = [
  ["search-tab-songs", "video-search-results"],
  ["search-tab-artists", "channel-search-results"],
];

function showSearchTab(selectedTabId) {
  for (const [tabId, listId] of SEARCH_TABS) {
    const on = tabId === selectedTabId;
    const tab = document.getElementById(tabId);
    const list = document.getElementById(listId);
    if (tab) {
      tab.classList.toggle("is-selected", on);
      tab.setAttribute("aria-selected", String(on));
    }
    if (list) list.hidden = !on;
  }
}

/** The count beside a tab's label — hidden rather than "0" while a search is
 *  still running, so the strip doesn't claim an answer it hasn't got. */
function setSearchTabCount(tabId, count) {
  const el = document.getElementById(`${tabId}-count`);
  if (!el) return;
  el.textContent = count == null ? "" : String(count);
  el.hidden = count == null;
}

// One input drives both endpoints in parallel, instead of two permanently
// visible search boxes.
export function setupExploreSearch() {
  const input = document.getElementById("explore-search-input");
  const resultsPanel = document.getElementById("explore-results-panel");
  const browsePanel = document.getElementById("explore-browse-panel");
  const videoResults = document.getElementById("video-search-results");
  const channelResults = document.getElementById("channel-search-results");
  if (!input || !resultsPanel || !browsePanel) return;

  setupSearchClear("explore-search-input", "explore-search-clear");

  // Results are an answer to a question, and the question is over once you've
  // acted on one of them. Leaving them up meant coming back to Explore later
  // and finding last week's search where the recommendations should be, with
  // no obvious way back other than noticing the clear button.
  //
  // Hung off the tab switch rather than off each result row: opening a result
  // activates the "detail" panel, which is a tab activation too, so one
  // listener covers both leaving Explore and drilling into something from it.
  function clearSearch() {
    if (!input.value) return;
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    videoResults.innerHTML = "";
    channelResults.innerHTML = "";
    // Back to Songs with the results themselves: the next search is a new
    // question, and answering it on whichever tab the last one ended on is
    // how someone finds themselves looking at an empty Artists list.
    showSearchTab("search-tab-songs");
    showExplorePanel("explore-browse-panel");
  }

  for (const [tabId] of SEARCH_TABS) {
    document.getElementById(tabId)?.addEventListener("click", () => showSearchTab(tabId));
  }

  onTabActivated((tab) => {
    if (tab !== "explore") clearSearch();
  });

  const runSearch = debounce(async (query) => {
    if (!query) {
      showExplorePanel("explore-browse-panel");
      videoResults.innerHTML = "";
      channelResults.innerHTML = "";
      return;
    }

    showExplorePanel("explore-results-panel");
    // Shown immediately, before either fetch resolves — without this, the
    // tab strip pops into view over empty lists the instant the debounce
    // fires, which reads as broken results rather than a pending search. The
    // two searches run in parallel and don't necessarily resolve together,
    // so each section clears its own placeholder.
    const loadingHtml = `<li class="search-loading"><span class="spinner"></span>Searching…</li>`;
    videoResults.innerHTML = loadingHtml;
    channelResults.innerHTML = loadingHtml;
    setSearchTabCount("search-tab-songs", null);
    setSearchTabCount("search-tab-artists", null);

    const [videos, channels] = await Promise.all([
      api(`/explore/songs?q=${encodeURIComponent(query)}`),
      api(`/explore/artists?q=${encodeURIComponent(query)}`),
    ]);

    if (videos.ok) {
      videoResults.innerHTML =
        videoRowsHtml(videos.data) || `<li class="search-empty">No songs found</li>`;
      setSearchTabCount("search-tab-songs", videos.data.length);
    }
    if (channels.ok) {
      renderChannelResults(channels.data);
      setSearchTabCount("search-tab-artists", channels.data.length);
    }
  }, 400);

  input.addEventListener("input", () => runSearch(input.value.trim()));

  channelResults.addEventListener("click", (event) => {
    // Add first: the row around it opens a preview instead, and the button
    // sits inside that row.
    const btn = event.target.closest(".btn-add-channel");
    if (btn) {
      followArtist(btn.dataset.channelUrl, btn);
      return;
    }

    // yt-artist, not yt-channel: searching an artist's name finds the
    // channel they upload to, and for an artist who also vlogs that listing
    // is mostly not music. Same id, same route as the shelves above — the
    // server tries the artist page first and falls back to these uploads
    // for a channel that isn't one (a podcast, say).
    const row = event.target.closest(".search-result-channel");
    if (row?.dataset.channelId) {
      openDetail("yt-artist", row.dataset.channelId);
    }
  });

  videoResults.addEventListener("click", (event) => {
    // Before the row, which is now clickable all the way across: the artist's
    // name sits inside it, and the row would otherwise swallow it and start
    // playing the song instead. Same order as the shelves above.
    const artistLink = event.target.closest(".artist-link");
    if (artistLink) {
      openDetail("yt-artist", artistLink.dataset.channelId);
      return;
    }

    // The whole row plays, not just the play button — a track in a list is
    // something you tap, and every other track list in this app already
    // behaves that way. The button stays for keyboard and screen-reader use
    // and is what gets disabled while the request is in flight.
    const row = event.target.closest(".video-search-result");
    if (!row) return;
    playRemoteVideo(row.dataset, row.querySelector(".video-search-play"));
  });
}
