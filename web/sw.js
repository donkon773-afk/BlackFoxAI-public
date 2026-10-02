/* BlackFox AI Workstation — service worker.

   Стратегия намеренно «сеть вперёд»: кластер живой, и устаревший интерфейс
   поверх обновлённого хаба — источник трудноуловимых ошибок. Кэш нужен
   только чтобы приложение открылось без связи и показало последнее
   состояние вместо пустого экрана. Запросы /api/ не кэшируются никогда. */

const CACHE = "blackfox-shell-v4";
const SHELL = [
  "/", "/style.css", "/mobile.css",
  "/app.js", "/views.js", "/sensors_view.js", "/models.js",
  "/admin.js", "/security.js", "/help.js", "/mobile.js", "/qr.js", "/connect.js",
  "/icons/icon-192.png", "/icons/icon-512.png", "/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;          // состояние кластера — только из сети

  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then(hit => hit || caches.match("/")))
  );
});
