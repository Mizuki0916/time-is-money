/* Time is Money — Service Worker
   ・アプリ本体(HTML)とデータ(JSON)はネット優先。更新したら次に開いたときすぐ反映される。
   ・アイコン等の変わらないファイルだけキャッシュ優先。
   ・圏外のときは最後に取得した内容を表示する。 */

const VERSION = 'v26';
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;

const PRECACHE = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
];

// 内容が変わらないファイル（キャッシュ優先でよいもの）
const STATIC = /\.(png|svg|ico|webmanifest)$/;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((c) => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

/** ネット優先。取れたらキャッシュを更新し、駄目なら前回の内容を返す。 */
function networkFirst(req, cacheName, fallback) {
  return fetch(req)
    .then((res) => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(cacheName).then((c) => c.put(req, copy));
      }
      return res;
    })
    .catch(() => caches.match(req).then((hit) => hit || (fallback ? caches.match(fallback) : undefined)));
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 画面遷移（アプリ本体）
  if (req.mode === 'navigate') {
    event.respondWith(networkFirst(req, SHELL, './index.html'));
    return;
  }

  // データ
  if (url.pathname.includes('/data/')) {
    event.respondWith(
      networkFirst(req, DATA).then((res) => res || Response.json(
        { error: 'offline', stocks: [], videos: [], channels: [], stats: {}, charts: {} },
        { status: 200 }
      ))
    );
    return;
  }

  // アイコンなど：キャッシュ優先
  if (STATIC.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // それ以外（index.html を直接指定した場合など）もネット優先
  event.respondWith(networkFirst(req, SHELL, './index.html'));
});
