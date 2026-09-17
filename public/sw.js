/* Parrot POS — Offline Service Worker.
 *
 * Provides a PWA app shell so the dashboard can be reloaded without a network
 * connection. Data freshness is network-first: when online the app always gets
 * live responses (and the cache is refreshed), when offline the last cached
 * copies are served. Only works in secure contexts (HTTPS or localhost); on a
 * plain-HTTP LAN the browser ignores this file and the localStorage data caches
 * in dashboard.html still provide offline POS data.
 */
const SW_VERSION = "1.0.0";
const CACHE_NAME = "parrot-pos-" + SW_VERSION;

const PRECACHE_URLS = [
  "/login",
  "/public/photos/logo.png",
  "/public/photos/logo.ico",
  "/public/vendor/bootstrap/bootstrap.min.css",
  "/public/vendor/bootstrap/bootstrap.bundle.min.js",
  "/public/vendor/bootstrap-icons/bootstrap-icons.css",
  "/public/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
  "/public/vendor/bootstrap-icons/fonts/bootstrap-icons.woff",
  "/public/vendor/chartjs/chart.umd.min.js",
  "/public/vendor/marked/marked.min.js",
  "/public/vendor/dompurify/purify.min.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  // Never intercept non-GET requests (sales, CRUD, etc.) — the client-side
  // offline sale queue in dashboard.html handles failures gracefully instead.
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // Cross-origin (e.g. Google Fonts): cache-first with runtime population.
  if (url.origin !== self.location.origin) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request)
          .then((response) => {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
            return response;
          })
          .catch(() => cached);
      })
    );
    return;
  }

  // Navigation: network-first, fall back to the cached shell offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() =>
          caches.match(request).then((cached) => cached || caches.match("/"))
        )
    );
    return;
  }

  // Same-origin GET (static assets + API): network-first with cache fallback.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});