/*
 * TrustNode Lite service worker.
 *
 * Caches the static shell (HTML/CSS/manifest/icon) for offline first-load,
 * NETWORK-only for everything else (every Supabase query goes to the wire so
 * users always see the freshest data).
 *
 * Versioning: bump CACHE_VERSION whenever the shell changes so old caches are
 * evicted on activate. Browsers re-check the service worker on each navigation
 * so users pick the new shell up automatically next time they open the app.
 */
const CACHE_VERSION = "tnlite-v2";
const SHELL = [
  "./",
  "./index.html",
  "./styles.css",
  "./manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // mutations bypass the SW
  const url = new URL(req.url);
  // Never cache data calls (supabase REST/realtime) or config.json.
  // We want every dashboard load to read live values.
  if (url.pathname.endsWith("/config.json") || url.host.endsWith(".supabase.co")) {
    return; // default network behaviour
  }
  // Same-origin static shell: cache-first with network fallback.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res && res.ok) {
          const clone = res.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(req, clone)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match("./index.html")))
    );
  }
});
