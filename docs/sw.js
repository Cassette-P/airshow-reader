/* ==========================================================================
   airshow-reader Service Worker (PWA Shell & Network-Direct Data Engine)
   ========================================================================== */

const CACHE_NAME = 'airshow-reader-v6';

const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './favicon.svg',
  './artwork-airshow-512.png',
  './artwork-sinica-512.png',
  './artwork-tal-512.png',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './css/tokens.css',
  './css/layout.css',
  './css/components.css',
  './js/main.js',
  './js/state.js',
  './js/storage.js',
  './js/api.js',
  './js/theme.js',
  './js/player.js',
  './js/reader.js',
  './js/archive.js',
  './js/shortcuts.js',
  './js/utils.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') {
    return;
  }

  const url = new URL(request.url);

  // 1. STRICT REQUIREMENT: Direct network for data requests & media
  // NO Cache-First for ./data/** to guarantee latest episodes are immediately visible.
  if (
    url.pathname.includes('/data/') ||
    request.url.includes('/data/') ||
    request.destination === 'audio' ||
    url.pathname.endsWith('.mp3')
  ) {
    event.respondWith(fetch(request));
    return;
  }

  // 2. Navigation / Document requests: Network-First with shell fallback
  if (request.mode === 'navigate' || request.destination === 'document') {
    event.respondWith(
      fetch(request)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const clone = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
          }
          return networkResponse;
        })
        .catch(() => caches.match(request).then((res) => res || caches.match('./index.html')))
    );
    return;
  }

  // 3. Static shell assets: Cache-First with Network fallback
  event.respondWith(
    caches.match(request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }
      return fetch(request).then((networkResponse) => {
        if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== 'basic') {
          return networkResponse;
        }
        const clone = networkResponse.clone();
        caches.open(CACHE_NAME).then((cache) => {
          cache.put(request, clone);
        });
        return networkResponse;
      });
    })
  );
});
