// Network-first service worker: exists for installability and an offline
// fallback, not offline-first — a cache-first strategy would fight no-cache static assets.
// Bump the version whenever an old cache may hold entries that must be purged.
const CACHE_NAME = "spotea-v6";

// Every module in the import graph must be here: one 404ing ES module blanks the
// whole page. tests/test_static_js.py holds this list to the files on disk.
// sw.js is deliberately absent — a worker serving itself from cache can't update.
const PRECACHE_URLS = [
  "/",
  "/static/css/style.css",
  "/static/manifest.json",
  "/static/img/apple-touch-icon.png",
  "/static/img/icons/icon-192.png",
  "/static/img/icons/icon-512.png",
  "/static/img/icons/icon-maskable-192.png",
  "/static/img/icons/icon-maskable-512.png",
  "/static/js/content-actions.js",
  "/static/js/core.js",
  "/static/js/fragments.js",
  "/static/js/offline.js",
  "/static/js/player.js",
  "/static/js/resume.js",
  "/static/js/viewport.js",
  "/static/js/home/ambient.js",
  "/static/js/home/detail.js",
  "/static/js/home/device.js",
  "/static/js/home/explore.js",
  "/static/js/home/library.js",
  "/static/js/home/lyrics.js",
  "/static/js/home/overlay.js",
  "/static/js/home/playlists.js",
  "/static/js/home/queue.js",
  "/static/js/home/remote.js",
  "/static/js/home/scrollers.js",
  "/static/js/home/settings.js",
  "/static/js/home/tabs.js",
  "/static/js/pages/index.js",
];

// Never cached: the Cache API ignores Range (breaks audio seeking), error bodies
// would be replayed as payloads, and per-profile data would leak across profile switches.
const API_PREFIXES = [
  "/content",
  "/feeds",
  // core.js polls this to detect reconnection; a cached answer would always say "online".
  "/health",
  "/profiles",
  "/settings",
  "/storage",
  "/partials",
  "/recommendations",
  "/onboarding",
];

// Bare routes like "/settings" have no trailing slash, so match the prefix
// exactly or followed by "/" — never a plain startsWith("/settings/").
function isApiPath(path) {
  return API_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

// An unreachable tailnet host (VPN off) hangs on the TCP handshake instead of
// rejecting, so fall back to cache after this long; the request keeps running.
const NETWORK_TIMEOUT_MS = 3000;

// After a timeout, serve cache without requesting: hung requests hold the ~6
// per-origin sockets and the shell's modules would queue behind them.
const UNREACHABLE_FOR_MS = 10000;

let unreachableUntil = 0;

function networkFirst(request) {
  const shortcut = Date.now() < unreachableUntil ? caches.match(request) : Promise.resolve(null);

  return shortcut.then((shortcutted) => {
    if (shortcutted) return shortcutted;

    const network = fetch(request).then((response) => {
      unreachableUntil = 0;
      const copy = response.clone();
      caches
        .open(CACHE_NAME)
        .then((cache) => cache.put(request, copy))
        .catch(() => {});
      return response;
    });

    return new Promise((resolve) => {
      let answered = false;
      const answer = (response) => {
        if (answered || !response) return;
        answered = true;
        resolve(response);
      };

      network.then(answer).catch(() => {
        // Resolving undefined on a cache miss surfaces an ordinary network error.
        if (answered) return;
        answered = true;
        caches.match(request).then(resolve);
      });

      setTimeout(() => {
        if (answered) return;
        unreachableUntil = Date.now() + UNREACHABLE_FOR_MS;
        caches.match(request).then(answer);
      }, NETWORK_TIMEOUT_MS);
    });
  });
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // Not cache.addAll: one flaky request would reject the whole install.
      Promise.allSettled(
        PRECACHE_URLS.map((url) =>
          fetch(url, { credentials: "same-origin" }).then((response) => {
            // An expired session redirects "/" to the login page with a 200;
            // caching it would pin the login screen as the offline home.
            if (!response.ok || response.redirected) return;
            return cache.put(url, response);
          })
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // Opaque no-cors responses (remote artwork) fail through the clone/cache path.
  if (url.origin !== self.location.origin) return;

  if (isApiPath(url.pathname)) return;

  event.respondWith(networkFirst(event.request));
});
