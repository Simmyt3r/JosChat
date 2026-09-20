const CACHE_NAME = "joschat-cache-v3";
const OFFLINE_URLS = [
  "/",
  "/static/js/app.js",
  "/static/js/background.js",
  "/static/css/style.css",
  "/static/manifest.json",
  "/static/favicon.ico",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  // Cache each file individually so one failing request can't abort the whole install.
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.all(OFFLINE_URLS.map((url) => cache.add(url).catch(() => {})))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin GETs. Never cache API calls (messages/auth must
  // stay fresh) and leave cross-origin traffic (Supabase, CDN, fonts) alone.
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return;
  }

  // Network first, cache as the offline fallback. (Cache-first would keep
  // serving an old app.js/index.html forever after a deploy.)
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
