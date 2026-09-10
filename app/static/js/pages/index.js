// Entry point for index.html — Home, Library, Explore, Settings, and the
// channel/playlist detail panel and player overlay drilled into from them.

import { watchConnection } from "../core.js";
import { setupAmbientTint } from "../home/ambient.js";
import { setupDeviceStorage, setupOfflineMode } from "../home/device.js";
import { handleInitialRoute, setupDetailPanel } from "../home/detail.js";
import { refreshRecommendations, setupExploreSearch, setupRecommendations } from "../home/explore.js";
import {
  setupHomeArtists,
  setupHorizontalScrollers,
  setupLibraryArtistGrid,
  setupLibrarySearch,
  setupMobileMenu,
  setupPreparingArtists,
  setupRefreshButton,
  setupSplash,
} from "../home/library.js";
import { setupLyricsPanel } from "../home/lyrics.js";
import { resumeOverlayIfNeeded, setupPlayerOverlay } from "../home/overlay.js";
import { setupPlaylists } from "../home/playlists.js";
import {
  setupDownloadsOverlay,
  setupInterests,
  setupSettings,
  setupStorage,
} from "../home/settings.js";
import { setupTabs } from "../home/tabs.js";
import { installVisibilityBreadcrumb, setupFavorite, setupPlayer } from "../player.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";
import { installHeaderOffset, installKeyboardInset } from "../viewport.js";

// First: dismissing the boot cover has nothing to do with the rest of setup
// below, and registering the `load` listener it waits on before anything
// else here runs is what keeps it accurate.
setupSplash();
installBfcacheReload();
registerServiceWorker();
installKeyboardInset();
installHeaderOffset();

setupTabs();
setupPlayer();
setupFavorite();
installVisibilityBreadcrumb();
setupPlayerOverlay();
// After setupPlayer: onPlayerEvent binds to the audio element that exists
// now, and setupPlayer is what puts it there.
// Before watchConnection, which raises the banner (and so announces the
// state) as its very first act — a listener registered after it would miss
// the one announcement that matters, the app opening with no connection.
setupOfflineMode();
// Before anything that might fail: offline, the banner is the context for
// every failure that follows it.
watchConnection();
setupLyricsPanel();
setupDetailPanel();
// After setupDetailPanel: both listen on #detail-panel, and this one's
// handler should not run for a click the panel has already acted on.
setupPlaylists();
resumeOverlayIfNeeded();
// resumeOverlayIfNeeded only reopens a track left playing in a previous
// session; a #channel/42 or #player/123 hash in the URL right now is a
// separate, higher-priority thing to resolve on boot.
handleInitialRoute();
setupExploreSearch();
// After the shelves exist and after setupTabs, which is what decides Home is
// the visible panel: the tint is sampled from an <img> that has to be laid
// out, and the header observer measures against a hero that has to be on
// screen to intersect anything.
setupAmbientTint();
setupRecommendations();
setupDownloadsOverlay();
setupDeviceStorage();
setupStorage();
setupSettings();
setupInterests();
setupHomeArtists();
setupLibraryArtistGrid();
setupLibrarySearch();
setupPreparingArtists();
setupHorizontalScrollers();
// The one Refresh control covers Explore's recommendations too — passed in
// rather than imported inside library.js, which explore.js already imports
// from (see setupRefreshButton).
setupRefreshButton(refreshRecommendations);
setupMobileMenu(refreshRecommendations);
