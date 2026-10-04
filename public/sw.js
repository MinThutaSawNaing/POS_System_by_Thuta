/* Parrot POS — Offline Service Worker.
 *
 * Cache only public static assets, never authenticated HTML or API responses.
 * Offline reload of the dashboard intentionally requires a network connection:
 * a shared device must not restore the previous user's role-sensitive shell.
 * An already-open screen can still use its account-scoped localStorage data.
 * Service workers require HTTPS or localhost.
 */
const SW_VERSION = "1.2.0";
const CACHE_NAME = "parrot-pos-" + SW_VERSION;

const PRECACHE_URLS = [
  "/public/photos/logo.png",
  "/public/photos/logo.ico",
  "/public/manifest.webmanifest",
  "/public/pwa/icon-192.png",
  "/public/pwa/icon-512.png",
  "/public/vendor/bootstrap/bootstrap.min.css",
  "/public/vendor/bootstrap/bootstrap.bundle.min.js",
  "/public/vendor/bootstrap-icons/bootstrap-icons.css",
  "/public/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
  "/public/vendor/bootstrap-icons/fonts/bootstrap-icons.woff",
  "/public/vendor/chartjs/chart.umd.min.js",
  "/public/vendor/marked/marked.min.js",
  "/public/vendor/dompurify/purify.min.js",
  "/public/vendor/fontawesome/css/all.min.css",
  "/public/vendor/fontawesome/webfonts/fa-brands-400.woff2",
  "/public/vendor/fontawesome/webfonts/fa-regular-400.woff2",
  "/public/vendor/fontawesome/webfonts/fa-solid-900.woff2",
  "/public/vendor/fontawesome/webfonts/fa-v4compatibility.woff2",
  "/public/vendor/fontawesome/webfonts/fa-brands-400.ttf",
  "/public/vendor/fontawesome/webfonts/fa-regular-400.ttf",
  "/public/vendor/fontawesome/webfonts/fa-solid-900.ttf",
  "/public/vendor/fontawesome/webfonts/fa-v4compatibility.ttf",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        // Cache each asset individually so one unreachable URL never aborts
        // the whole install (cache.addAll would reject the entire list).
        Promise.allSettled(
          PRECACHE_URLS.map((url) =>
            fetch(url, { cache: "reload" })
              .then((response) => {
                if (response && response.status === 200 && !response.redirected &&
                    !/no-store|private/i.test(response.headers.get("Cache-Control") || "")) {
                  return cache.put(url, response.clone());
                }
              })
          )
        )
      )
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

  // Default deny: all navigation (including /), API, receipts and reports use
  // the network only and bypass the HTTP cache as well as Cache Storage.
  // Explicitly allow only known public assets; query variants are not cached.
  if (url.origin !== self.location.origin || request.mode === "navigate" ||
      url.search || !PRECACHE_URLS.includes(url.pathname)) {
    event.respondWith(fetch(request, { cache: "no-store" }));
    return;
  }

  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      const cached = await cache.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok && !response.redirected &&
          !/no-store|private/i.test(response.headers.get("Cache-Control") || "")) {
        await cache.put(request, response.clone());
      }
      return response;
    })
  );
});