/* ZivoIT Field Tracker - service worker (scope /zivoit_tracking/)
 * 1. Caches the app shell so the tracker opens with no network at all.
 * 2. Background Sync: uploads queued points from IndexedDB when the
 *    connection returns, even if the app tab was closed in the meantime.
 */
"use strict";

var CACHE = "zivo-tracker-v19.0.1.0.0";
var SHELL = [
    "/zivoit_tracking/app",
    "/zivoit_tracking/manifest.webmanifest",
    "/zivoit_field_tracking/static/app/app.js",
    "/zivoit_field_tracking/static/app/app.css",
    "/zivoit_field_tracking/static/app/icons/icon-192.png",
    "/zivoit_field_tracking/static/app/icons/icon-512.png",
];

self.addEventListener("install", function (event) {
    event.waitUntil(
        caches.open(CACHE).then(function (cache) {
            return Promise.all(SHELL.map(function (url) {
                return fetch(url, { credentials: "same-origin" }).then(function (resp) {
                    if (resp.ok && !resp.redirected) { return cache.put(url, resp); }
                }).catch(function () {});
            }));
        }).then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener("activate", function (event) {
    event.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys.filter(function (k) { return k.indexOf("zivo-tracker-") === 0 && k !== CACHE; })
                .map(function (k) { return caches.delete(k); }));
        }).then(function () { return self.clients.claim(); })
    );
});

function isShell(url) {
    return SHELL.indexOf(url.pathname) !== -1;
}

self.addEventListener("fetch", function (event) {
    var req = event.request;
    if (req.method !== "GET") { return; }
    var url = new URL(req.url);
    if (url.origin !== self.location.origin) { return; }
    if (url.pathname.indexOf("/zivoit_tracking/api/") === 0) { return; } // never cache API calls
    if (!isShell(url)) { return; }
    var key = url.pathname;
    // network first (fresh code and fresh device token), cache fallback when offline
    event.respondWith(
        fetch(req).then(function (resp) {
            if (resp.ok && !resp.redirected && resp.type === "basic") {
                var copy = resp.clone();
                caches.open(CACHE).then(function (cache) { cache.put(key, copy); });
            }
            return resp;
        }).catch(function () {
            return caches.match(key).then(function (hit) {
                return hit || new Response("<h1>Offline</h1><p>Open the tracker once while online.</p>",
                    { status: 503, headers: { "Content-Type": "text/html" } });
            });
        })
    );
});

// ---------------------------------------------------------------- background sync
function idb() {
    return new Promise(function (resolve, reject) {
        var req = indexedDB.open("zivo_field_tracker", 1);
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { reject(req.error); };
        req.onupgradeneeded = function () { req.transaction.abort(); };
    });
}
function idbAll(d, store, limit) {
    return new Promise(function (resolve, reject) {
        var r = d.transaction(store, "readonly").objectStore(store).getAll(null, limit || undefined);
        r.onsuccess = function () { resolve(r.result); };
        r.onerror = function () { reject(r.error); };
    });
}
function idbGet(d, store, key) {
    return new Promise(function (resolve, reject) {
        var r = d.transaction(store, "readonly").objectStore(store).get(key);
        r.onsuccess = function () { resolve(r.result); };
        r.onerror = function () { reject(r.error); };
    });
}
function idbDel(d, store, keys) {
    return new Promise(function (resolve, reject) {
        var t = d.transaction(store, "readwrite");
        var s = t.objectStore(store);
        keys.forEach(function (k) { if (k) { s.delete(k); } });
        t.oncomplete = resolve; t.onerror = function () { reject(t.error); };
    });
}

function flushQueue() {
    return idb().then(function (d) {
        return Promise.all([idbGet(d, "kv", "token"), idbAll(d, "points", 500), idbAll(d, "events")]).then(function (r) {
            var token = r[0], points = r[1], events = r[2];
            if (!token || (!points.length && !events.length)) { return; }
            return fetch("/zivoit_tracking/api/sync", {
                method: "POST", credentials: "omit",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
                body: JSON.stringify({ points: points, visits: [], events: events }),
            }).then(function (resp) {
                if (!resp.ok) { throw new Error("http_" + resp.status); }
                return resp.json();
            }).then(function (data) {
                var p = data.points || {};
                var ack = (p.accepted || []).concat(p.duplicates || [], (p.rejected || []).map(function (x) { return x.uid; }));
                return Promise.all([idbDel(d, "points", ack), idbDel(d, "events", (data.events || {}).accepted || [])]);
            }).then(function () {
                return self.clients.matchAll().then(function (clients) {
                    clients.forEach(function (c) { c.postMessage({ type: "zivo-synced" }); });
                });
            });
        });
    });
}

self.addEventListener("sync", function (event) {
    if (event.tag === "zivo-sync") { event.waitUntil(flushQueue()); }
});
self.addEventListener("message", function (event) {
    if (event.data && event.data.type === "zivo-flush") { event.waitUntil(flushQueue()); }
});
