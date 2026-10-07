const CACHE = "anki-lan-shell-26.9.3-v4";
const SHELL = [
  "/shell/static/mobile.css",
  "/shell/static/bootstrap.js",
  "/shell/static/manifest.webmanifest",
  "/shell/static/icon.svg",
  "/shell/static/spa-nav.css?v=4",
  "/shell/static/spa-nav.js?v=4"
];
self.addEventListener("install", event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())
));
self.addEventListener("activate", event => event.waitUntil(
  caches.keys()
    .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
    .then(() => self.clients.claim())
));
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !url.pathname.startsWith("/shell/static/")) return;
  event.respondWith(
    fetch(event.request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request, copy)));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
