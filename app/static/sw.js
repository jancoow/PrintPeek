// Makes the site installable as an app and shows an offline page when the server can't be reached.
// Only page loads and /static/ files go through here, always from the network first. The API,
// camera streams and videos are never touched or cached.
const CACHE = "printer-monitor-v1";
const OFFLINE = "/static/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll([OFFLINE, "/static/style.css", "/static/icons/icon.svg"]))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));
  } else if (new URL(request.url).pathname.startsWith("/static/")) {
    event.respondWith(fetch(request).catch(() => caches.match(request)));
  }
});
