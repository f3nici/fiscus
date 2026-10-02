/*
 * Fiscus service worker. VERSION and SHELL are prepended by the server.
 *
 * It caches the application shell and nothing else: /static/* and the offline
 * screen. Every page, every form post and every HTMX fragment carries someone's
 * money, so those always go to the network and are never stored here. When the
 * network is gone a page load gets the offline screen, never yesterday's
 * figures.
 */
const CACHE = 'fiscus-shell-' + VERSION;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)),
  );
  // No skipWaiting here: the page asks the person first (see app.js).
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k.startsWith('fiscus-shell-') && k !== CACHE).map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'SKIP_WAITING') self.skipWaiting();
  if (data.type === 'TAKE_SHARED_FILE') {
    const file = sharedFile;
    sharedFile = null;
    event.source.postMessage({ type: 'SHARED_FILE', file });
  }
});

/** Only these are ever answered from the cache. */
function isShell(url) {
  return url.origin === self.location.origin && (url.pathname.startsWith('/static/') || url.pathname === '/offline');
}

// A CSV shared from another app (Android share sheet). Held in memory only,
// handed to the import page once, then dropped. Never written to a cache.
let sharedFile = null;

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.method === 'POST' && url.pathname === '/share-target') {
    event.respondWith(
      (async () => {
        try {
          const form = await request.formData();
          const file = form.get('file');
          sharedFile = file && typeof file !== 'string' ? file : null;
        } catch (e) {
          sharedFile = null;
        }
        return Response.redirect('/import?shared=1', 303);
      })(),
    );
    return;
  }

  if (request.method !== 'GET') return;

  if (isShell(url)) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then((hit) => hit || fetch(request)),
      ),
    );
    return;
  }

  if (request.mode === 'navigate') {
    // Network only. Offline gets the offline screen, not a cached page.
    event.respondWith(
      fetch(request).catch(() => caches.open(CACHE).then((cache) => cache.match('/offline'))),
    );
  }
  // Everything else (HTMX fragments, JSON, downloads) is left to the browser,
  // and every one of those responses is sent Cache-Control: no-store.
});
