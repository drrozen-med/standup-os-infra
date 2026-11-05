// Navigator offline layer. Keeps the app shell and the last good copy of every read-only API answer
// (sessions, decisions, questions, chat/terminal transcripts) so the phone can still read them when the
// Mac is asleep, out of cover, or the phone itself has no signal. Writes are never cached here; the page
// queues the few it can replay later (see offline.js).
const SHELL_CACHE = 'nav-shell-v1';
const DATA_CACHE = 'nav-data-v1';
const SHELL = ['/', '/__nav/chat.js', '/__nav/chat.css', '/__nav/ccc-skin.css', '/__nav/offline.js',
  '/__nav/manifest.webmanifest', '/__nav/icon-192.png', '/__nav/apple-touch-icon.png'];
const MAX_DATA_ENTRIES = 400;
const NETWORK_WAIT_MS = 6000;
// never answered from cache: live probes, audio, pairing, anything that must reflect the real network
const SKIP = [/^\/api\/voice\//, /^\/__pair/, /^\/api\/remote\//, /^\/api\/version/, /^\/api\/upload/, /^\/sw\.js/];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(SHELL_CACHE);
    await Promise.all(SHELL.map((u) => c.add(new Request(u, { cache: 'reload' })).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (![SHELL_CACHE, DATA_CACHE].includes(k)) await caches.delete(k);
    await self.clients.claim();
  })());
});

function stamp(res, fromCache) {
  // wrap so the page can see when this copy was saved and that it did not come from the live server
  const h = new Headers(res.headers);
  if (!fromCache) h.set('x-nav-cached-at', String(Date.now()));
  else h.set('x-nav-from-cache', '1');
  return h;
}

async function save(cacheName, req, res) {
  if (!res || !res.ok) return;
  const c = await caches.open(cacheName);
  const body = await res.clone().blob();
  await c.put(req, new Response(body, { status: res.status, statusText: res.statusText, headers: stamp(res, false) }));
  if (cacheName === DATA_CACHE) {
    const keys = await c.keys();
    for (let i = 0; i < keys.length - MAX_DATA_ENTRIES; i++) await c.delete(keys[i]);   // oldest first
  }
}

async function fromCache(cacheName, req) {
  const c = await caches.open(cacheName);
  const hit = await c.match(req, { ignoreVary: true });
  if (!hit) return null;
  const body = await hit.blob();
  return new Response(body, { status: 200, headers: stamp(hit, true) });
}

// Ask the network, but give up waiting after NETWORK_WAIT_MS so the saved copy shows fast. The request
// keeps running in the background and refreshes the saved copy if it eventually succeeds.
function networkFirst(event, cacheName, req, keyReq) {
  const net = fetch(req).then(async (res) => {
    if (res.ok) event.waitUntil(save(cacheName, keyReq, res.clone()));
    return res;
  });
  const timer = new Promise((resolve) => setTimeout(() => resolve(null), NETWORK_WAIT_MS));
  return Promise.race([net.catch(() => null), timer]).then(async (res) => {
    if (res && res.status < 500) return res;               // a real answer (200, 401, 404...) wins
    const cached = await fromCache(cacheName, keyReq);
    if (cached) return cached;
    if (res) return res;
    try { return await net; } catch (e) {
      return new Response(JSON.stringify({ offline: true, error: 'offline and nothing saved yet' }),
        { status: 503, headers: { 'content-type': 'application/json', 'x-nav-offline': '1' } });
    }
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  if (req.headers.has('range') || SKIP.some((r) => r.test(url.pathname))) return;

  if (req.mode === 'navigate' || url.pathname === '/') {
    const key = new Request('/');
    event.respondWith(networkFirst(event, SHELL_CACHE, req, key).then(async (res) => {
      if (res.ok || res.headers.get('x-nav-from-cache')) return res;
      return (await fromCache(SHELL_CACHE, key)) || res;   // Worker "reconnecting" page (503) -> saved app
    }));
    return;
  }
  if (url.pathname.startsWith('/__nav/')) {                // static files: fresh first so a new version shows at once
    event.respondWith(networkFirst(event, SHELL_CACHE, req, req));
    return;
  }
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/nav/v1/')) {
    event.respondWith(networkFirst(event, DATA_CACHE, req, req));
  }
});
