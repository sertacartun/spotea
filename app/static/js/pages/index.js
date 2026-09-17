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
  setupInterests,
  setupSettings,
  setupStorage,
} from "../home/settings.js";
import { setupTabs } from "../home/tabs.js";
import { installVisibilityBreadcrumb, setupFavorite, setupPlayer } from "../player.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";
import { installHeaderOffset, installKeyboardInset } from "../viewport.js";

// First, so its `load` listener is registered before anything else runs.
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
// After setupPlayer, which creates the audio element onPlayerEvent binds to; before
// watchConnection, whose first act announces the offline state.
setupOfflineMode();
// Early: offline, the banner is the context for every failure that follows.
watchConnection();
setupLyricsPanel();
setupDetailPanel();
// After setupDetailPanel: both listen on #detail-panel, and this one must run second.
setupPlaylists();
resumeOverlayIfNeeded();
// After resume: a route hash in the URL takes priority over the previous session's track.
handleInitialRoute();
setupExploreSearch();
// After setupTabs: the tint samples a laid-out <img> and the header observer needs the visible panel.
setupAmbientTint();
setupRecommendations();
setupDeviceStorage();
setupStorage();
setupSettings();
setupInterests();
setupHomeArtists();
setupLibraryArtistGrid();
setupLibrarySearch();
setupPreparingArtists();
setupHorizontalScrollers();
setupRefreshButton(refreshRecommendations);
setupMobileMenu(refreshRecommendations);
