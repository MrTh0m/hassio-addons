/* Service worker OCPP Server — coquille applicative (PWA).
 *
 * Stratégie :
 *  - Navigation (le HTML) : réseau d'abord, on garde une copie ("app-shell")
 *    pour un affichage hors-ligne. Évite de servir une vieille version tant
 *    que le réseau répond.
 *  - Assets statiques (icônes, manifeste) : cache d'abord, réseau ensuite.
 *  - API et WebSocket : jamais mis en cache (données temps réel).
 *
 * Note : sous l'ingress Home Assistant, le SW s'enregistre dans le sous-chemin
 * de l'ingress. Le mode installable/hors-ligne complet fonctionne surtout via
 * un accès HTTPS (Nabu Casa, reverse-proxy) ou en localhost. Une éventuelle
 * erreur d'enregistrement est sans conséquence : l'appli fonctionne comme avant.
 */
const CACHE = "ocpp-shell-v0.19.18";
const SHELL = ["icon.svg", "icon.png", "manifest.webmanifest"];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {}))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Ne jamais intercepter l'API ni le service worker lui-même.
  if (url.pathname.includes("/api/") || url.pathname.endsWith("/sw.js")) return;

  // Navigation : réseau d'abord, repli sur la coquille mise en cache.
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const resp = await fetch(req);
          const cache = await caches.open(CACHE);
          cache.put("app-shell", resp.clone());
          return resp;
        } catch (e) {
          const cache = await caches.open(CACHE);
          const cached = await cache.match("app-shell");
          return cached || Response.error();
        }
      })()
    );
    return;
  }

  // Assets statiques : cache d'abord.
  const isAsset =
    url.pathname.endsWith(".svg") ||
    url.pathname.endsWith(".png") ||
    url.pathname.endsWith(".webmanifest");
  if (!isAsset) return;

  event.respondWith(
    caches.match(req).then(
      (cached) =>
        cached ||
        fetch(req)
          .then((resp) => {
            if (resp.ok) {
              const copy = resp.clone();
              caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return resp;
          })
          .catch(() => cached)
    )
  );
});
