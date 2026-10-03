const CACHE = "anki-lan-shell-v1";
const SHELL = [
  "/shell/static/mobile.css",
  "/shell/static/bootstrap.js",
  "/shell/static/manifest.webmanifest",
  "/shell/static/icon.svg"
];
self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener("activate", event => event.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
));
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !url.pathname.startsWith("/shell/static/")) return;
  event.respondWith(caches.match(event.request).then(hit => hit || fetch(event.request)));
});
