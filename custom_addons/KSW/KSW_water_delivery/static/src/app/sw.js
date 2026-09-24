/* KSW Water Delivery — the driver's app, offline.
 *
 * Scope is /water/ and nothing else: the Odoo web client keeps its own worker
 * and its own behaviour, and this one must never touch it.
 *
 * Two caches, because the two halves fail differently:
 *  - the SHELL (the page and its two assets) is precached on install and
 *    served cache-first, so a cold launch with no signal still starts. This is
 *    the whole reason the app lives outside the web client: core's worker
 *    keeps the session info it needs to replay a cached page in a module-level
 *    variable, and the browser kills an idle worker within seconds.
 *  - the DATA calls are network-only. A stale client list is served from
 *    IndexedDB by the app itself, which knows how old it is; a service worker
 *    replaying a cached JSON-RPC response would hide that.
 *
 * Nothing here queues or retries. The queue is the app's, in IndexedDB, because
 * it has to survive the worker being killed, the tab being closed and the phone
 * being restarted — and because a capture that has not reached the server yet
 * is the driver's business, not an invisible background fact.
 */
const VERSION = "__APP_VERSION__";
const SHELL_CACHE = `ksw-water-shell-${VERSION}`;

const SHELL = [
    "/water/",
    `/KSW_water_delivery/static/src/app/app.css?v=${VERSION}`,
    `/KSW_water_delivery/static/src/app/app.js?v=${VERSION}`,
    "/KSW_water_delivery/static/description/icon.svg",
    "/water/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        (async () => {
            const cache = await caches.open(SHELL_CACHE);
            // Individually, not addAll: addAll is all-or-nothing, so one asset
            // 404ing after a rename would leave the driver with no app at all
            // rather than with a missing icon.
            await Promise.all(
                SHELL.map((url) =>
                    cache.add(new Request(url, { cache: "reload" })).catch(() => {})
                )
            );
            await self.skipWaiting();
        })()
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        (async () => {
            const names = await caches.keys();
            await Promise.all(
                names
                    .filter((name) => name.startsWith("ksw-water-shell-") && name !== SHELL_CACHE)
                    .map((name) => caches.delete(name))
            );
            await self.clients.claim();
        })()
    );
});

const isDataCall = (url) =>
    url.pathname === "/water/sync" ||
    url.pathname === "/water/reference" ||
    url.pathname === "/water/recent";

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") {
        return; // /water/sync is a POST: straight to the network, or it fails
    }
    const url = new URL(request.url);
    if (url.origin !== self.location.origin || isDataCall(url)) {
        return;
    }

    // A navigation to /water/ must work with the radio off, so the cache
    // answers first and the network only refreshes it for next time.
    if (request.mode === "navigate") {
        event.respondWith(
            (async () => {
                const cache = await caches.open(SHELL_CACHE);
                const cached = await cache.match("/water/");
                const network = fetch(request)
                    .then((response) => {
                        if (response.ok) {
                            cache.put("/water/", response.clone());
                        }
                        return response;
                    })
                    .catch(() => null);
                return cached || (await network) || Response.error();
            })()
        );
        return;
    }

    if (url.pathname.startsWith("/KSW_water_delivery/static/")) {
        event.respondWith(
            (async () => {
                const cache = await caches.open(SHELL_CACHE);
                const cached = await cache.match(request, { ignoreSearch: false });
                if (cached) {
                    return cached;
                }
                try {
                    const response = await fetch(request);
                    if (response.ok) {
                        cache.put(request, response.clone());
                    }
                    return response;
                } catch (error) {
                    // Try again ignoring the ?v= cache-buster: a version bump
                    // that has not finished installing must not black out an
                    // app the driver is standing in front of a client with.
                    const loose = await cache.match(request, { ignoreSearch: true });
                    if (loose) {
                        return loose;
                    }
                    throw error;
                }
            })()
        );
    }
});

// The app asks for this after a successful flush so the badge and the queue
// count stay right in every open window, not just the one that did the sending.
self.addEventListener("message", (event) => {
    if (event.data === "ksw-water-skip-waiting") {
        self.skipWaiting();
    }
});
