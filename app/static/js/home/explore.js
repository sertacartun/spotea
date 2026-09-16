import { api, debounce, escapeHtml, formatDuration, setupSearchClear } from "../core.js";
import { openDetail } from "./detail.js";
import { wireScrollers } from "./scrollers.js";
import { followArtist, playRemoteVideo } from "./remote.js";
import { onTabActivated } from "./tabs.js";

// "monthly listeners" to match how Library labels the same subscriber_count figure.
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

/** Plain text when there's no channel id: yt-dlp fallback search results don't
 *  reliably carry one, and a link that opens nothing is worse than no link. */
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

function recVideoCardHtml(video) {
  const thumb = video.thumbnail_url
    ? `<img src="${escapeHtml(video.thumbnail_url)}" alt="" loading="lazy" />`
    : "";
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

/** No Follow button: a chart entry's id is the artist's "Topic" channel, not the
 *  one they upload to, so following from here would follow the wrong thing. */
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

/** Only "Moods", no genres: ytmusicapi fails to parse every Genres category
 *  (see music.MOOD_SECTION). */
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
    // Moods first: the only shelf that isn't empty on a brand-new library.
    moodsShelfHtml(data.moods),
    // lead: sized larger so Explore isn't identical rows stacked on each other.
    shelfHtml("Songs", data.videos, recVideoCardHtml, true),
    shelfHtml("Playlists", data.playlists, recPlaylistCardHtml),
    shelfHtml("Artists you may like", data.similar_artists, artistCardHtml),
    shelfHtml("Charts", data.charts, recPlaylistCardHtml),
    shelfHtml("Charting artists", data.chart_artists, artistCardHtml),
  ].join("");

  body.innerHTML =
    shelves ||
    `<p class="muted">Nothing came back this time. Try refreshing, or add a different interest in Settings.</p>`;
  // Shelves built here skip the fragment swap that normally wires drag-scrolling.
  wireScrollers();
}

// Skip re-rendering an identical batch (the normal case): replacing every <img>
// flashes empty thumbnails.
let renderedPayload = null;

let inFlight = null;

/**
 * Only the boot fetch and the app-wide Refresh pass `placeholder`; opening
 * Explore re-checks quietly so the tab never blanks out.
 */
async function loadRecommendations({ force = false, placeholder = false } = {}) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;
  if (inFlight && !force) return inFlight;

  if (document.body.classList.contains("is-offline")) {
    if (placeholder) {
      body.innerHTML = `<p class="muted">Recommendations need a connection — you're offline.</p>`;
    }
    return;
  }

  if (placeholder) {
    body.innerHTML = `<p class="search-loading"><span class="spinner"></span>Finding things you might like…</p>`;
  }

  const load = (async () => {
    const { ok, data } = await api(force ? "/recommendations/refresh" : "/recommendations", {
      method: force ? "POST" : "GET",
      errorMessage: "Could not load recommendations",
    });

    if (!ok) {
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

export function reloadRecommendations() {
  return loadRecommendations();
}

export async function refreshRecommendations() {
  await loadRecommendations({ force: true, placeholder: true });
}

export function setupRecommendations() {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  loadRecommendations({ placeholder: true });

  onTabActivated((tab) => {
    if (tab === "explore") loadRecommendations();
  });

  body.addEventListener("click", (event) => {
    // Before the card handlers: artist names sit inside clickable cards.
    const artistLink = event.target.closest(".artist-link");
    if (artistLink) {
      openDetail("yt-artist", artistLink.dataset.channelId);
      return;
    }

    const addButton = event.target.closest(".btn-add-channel");
    if (addButton) {
      followArtist(addButton.dataset.channelUrl, addButton);
      return;
    }

    const channelCard = event.target.closest(".shelf-channel-card");
    if (channelCard) {
      openDetail("yt-artist", channelCard.dataset.channelId);
      return;
    }

    const moodChip = event.target.closest(".channel-chip[data-mood-params]");
    if (moodChip) {
      openDetail("yt-mood", moodChip.dataset.moodParams, { title: moodChip.dataset.moodTitle });
      return;
    }

    const card = event.target.closest(".rec-card");
    if (!card) return;
    if (card.dataset.videoId) playRemoteVideo(card.dataset, card.querySelector(".rec-play"));
    else if (card.dataset.playlistId) openDetail("yt-playlist", card.dataset.playlistId);
  });
}

function showExplorePanel(visibleId) {
  for (const panelId of ["explore-browse-panel", "explore-results-panel"]) {
    const panel = document.getElementById(panelId);
    if (panel) panel.hidden = panelId !== visibleId;
  }
  // The tab strip lives in the shared sticky head, not inside the results panel.
  const tabs = document.getElementById("explore-search-tabs");
  if (tabs) tabs.hidden = visibleId !== "explore-results-panel";
}

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

function setSearchTabCount(tabId, count) {
  const el = document.getElementById(`${tabId}-count`);
  if (!el) return;
  el.textContent = count == null ? "" : String(count);
  el.hidden = count == null;
}

export function setupExploreSearch() {
  const input = document.getElementById("explore-search-input");
  const resultsPanel = document.getElementById("explore-results-panel");
  const browsePanel = document.getElementById("explore-browse-panel");
  const videoResults = document.getElementById("video-search-results");
  const channelResults = document.getElementById("channel-search-results");
  if (!input || !resultsPanel || !browsePanel) return;

  setupSearchClear("explore-search-input", "explore-search-clear");

  // Hung off tab activation: opening a result activates the "detail" panel, so
  // one listener covers leaving Explore and drilling into a result.
  function clearSearch() {
    if (!input.value) return;
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    videoResults.innerHTML = "";
    channelResults.innerHTML = "";
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
    // Add first: the button sits inside the row, which opens a preview instead.
    const btn = event.target.closest(".btn-add-channel");
    if (btn) {
      followArtist(btn.dataset.channelUrl, btn);
      return;
    }

    // yt-artist: the server tries the artist page and falls back to uploads.
    const row = event.target.closest(".search-result-channel");
    if (row?.dataset.channelId) {
      openDetail("yt-artist", row.dataset.channelId);
    }
  });

  videoResults.addEventListener("click", (event) => {
    // Before the row: the artist's name sits inside it.
    const artistLink = event.target.closest(".artist-link");
    if (artistLink) {
      openDetail("yt-artist", artistLink.dataset.channelId);
      return;
    }

    const row = event.target.closest(".video-search-result");
    if (!row) return;
    playRemoteVideo(row.dataset, row.querySelector(".video-search-play"));
  });
}
