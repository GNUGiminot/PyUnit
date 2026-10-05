/* Service worker: только оболочка приложения.
   Данные сушилки НИКОГДА не кэшируются — показать устаревшую температуру
   опаснее, чем показать «нет связи». */
var CACHE = 'dryer-shell-v1';
var SHELL = [
  './', './index.html', './app.css', './app.js',
  './manifest.webmanifest',
  './icons/icon-192.png', './icons/icon-512.png'
];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () {
    return self.skipWaiting();
  }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) { return k === CACHE ? null : caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // API и счётчик — только из сети, без запасного варианта из кэша
  if (url.pathname.indexOf('/api/') === 0 || url.pathname.indexOf('/stats') === 0) return;
  if (url.origin !== location.origin) return;

  // Оболочка: сначала сеть (чтобы обновления доезжали), кэш — запасной путь
  e.respondWith(
    fetch(e.request).then(function (r) {
      if (r && r.ok) {
        var copy = r.clone();
        caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
      }
      return r;
    }).catch(function () {
      return caches.match(e.request).then(function (hit) {
        return hit || caches.match('./index.html');
      });
    })
  );
});
