const CACHE_NAME = 'meetings-v17-batch2-collapsible';

// Minimal service worker for PWA installability.
// Only caches the app shell; API calls always go to network.
const SHELL_ASSETS = [
  '/',
  '/static/styles.css',
  '/static/queue-logic.js',
  '/static/app.js',
  '/static/floating-chat.js',
  '/static/notes-tasks.css',
  '/static/notes-sync-logic.js',
  '/static/notes-sync.js',
  '/static/notes-tasks.js',
  '/static/notes-analysis-logic.js',
  '/static/capture-notes-logic.js',
  '/static/vendor/codemirror.bundle.js',
  '/static/icons/favicon.svg',
  '/static/icons/favicon-32.png',
  '/static/icons/favicon-16.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Notes GETs (export / list / single) get a runtime cache as a first-paint
  // fallback when offline. The IndexedDB mirror remains the real offline source
  // of truth; this only helps the very first paint before the mirror is warm.
  if (event.request.method === 'GET'
      && (url.pathname === '/api/notes/export' || url.pathname === '/api/notes'
          || url.pathname.startsWith('/api/notes/'))
      && !url.pathname.startsWith('/api/notes/attachments/')) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          // Only cache good responses: a 304 (empty body, now that export is
          // conditional) or an error stored here would poison the offline
          // first-paint fallback. Non-ok responses pass through untouched;
          // the last good 200 stays cached for offline.
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Always use network for other API calls and uploads
  if (url.pathname.startsWith('/meetings') || url.pathname.startsWith('/api')) {
    return;
  }

  // Network-first for everything else, fall back to cache
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
