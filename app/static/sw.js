// Makes the site installable as an app, shows an offline page when the server can't be reached,
// and shows push notifications.
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

self.addEventListener("push", (event) => {
  const msg = event.data ? event.data.json() : {};
  event.waitUntil(self.registration.showNotification(msg.title || "Printer monitor", {
    body: msg.body || "",
    tag: msg.tag || undefined,
    renotify: Boolean(msg.tag), // a newer message about the same printer replaces the old one, and still buzzes
    image: msg.image,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    data: { url: msg.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((w) => w.url.startsWith(self.location.origin));
    if (!open) return self.clients.openWindow(url);
    await open.focus();
    if (open.url !== url && "navigate" in open) await open.navigate(url);
  })());
});
