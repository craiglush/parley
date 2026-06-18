const CACHE_NAME = 'meetings-v8-task-crud';

// Minimal service worker for PWA installability.
// Only caches the app shell; API calls always go to network.
const SHELL_ASSETS = [
  '/',
  '/static/styles.css',
  '/static/app.js',
  '/static/floating-chat.js',
  '/static/notes-tasks.css',
  '/static/notes-tasks.js',
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

  // Always use network for API calls and uploads
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
