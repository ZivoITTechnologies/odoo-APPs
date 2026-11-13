/* ZivoIT Field Tracker - offline-first GPS tracking web app for Odoo.
 * Copyright 2026 ZivoIT Technologies - License OPL-1
 *
 * Every GPS fix is written to IndexedDB on the phone first, then uploaded in
 * batches. The server acknowledges each point id; only acknowledged points are
 * removed, so nothing is lost when the network drops in the middle of a sync,
 * and re-sent points are de-duplicated by the server.
 */
(function () {
    "use strict";

    var DB_NAME = "zivo_field_tracker";
    var DB_VERSION = 1;
    var SYNC_URL = "/zivoit_tracking/api/sync";
    var CUSTOMERS_URL = "/zivoit_tracking/api/customers";
    var BATCH = 200;
    var HEARTBEAT_MS = 5 * 60 * 1000;

    var boot = {};
    try { boot = JSON.parse(document.body.getAttribute("data-boot") || "{}"); } catch (e) { boot = {}; }

    // ------------------------------------------------------------------ IndexedDB
    var dbPromise = null;
    function db() {
        if (!dbPromise) {
            dbPromise = new Promise(function (resolve, reject) {
                var req = indexedDB.open(DB_NAME, DB_VERSION);
                req.onupgradeneeded = function () {
                    var d = req.result;
                    if (!d.objectStoreNames.contains("points")) { d.createObjectStore("points", { keyPath: "uid" }); }
                    if (!d.objectStoreNames.contains("visits")) { d.createObjectStore("visits", { keyPath: "uid" }); }
                    if (!d.objectStoreNames.contains("events")) { d.createObjectStore("events", { keyPath: "uid" }); }
                    if (!d.objectStoreNames.contains("kv")) { d.createObjectStore("kv"); }
                };
                req.onsuccess = function () { resolve(req.result); };
                req.onerror = function () { reject(req.error); };
            });
        }
        return dbPromise;
    }
    function tx(store, mode, fn) {
        return db().then(function (d) {
            return new Promise(function (resolve, reject) {
                var t = d.transaction(store, mode);
                var s = t.objectStore(store);
                var out = fn(s);
                t.oncomplete = function () { resolve(out instanceof IDBRequest ? out.result : undefined); };
                t.onerror = function () { reject(t.error); };
                t.onabort = function () { reject(t.error); };
            });
        });
    }
    function put(store, value, key) { return tx(store, "readwrite", function (s) { return key === undefined ? s.put(value) : s.put(value, key); }); }
    function get(store, key) { return tx(store, "readonly", function (s) { return s.get(key); }); }
    function getAll(store, limit) { return tx(store, "readonly", function (s) { return s.getAll(null, limit || undefined); }); }
    function count(store) { return tx(store, "readonly", function (s) { return s.count(); }); }
    function delMany(store, keys) {
        if (!keys.length) { return Promise.resolve(); }
        return tx(store, "readwrite", function (s) { keys.forEach(function (k) { if (k) { s.delete(k); } }); });
    }

    // ------------------------------------------------------------------ helpers
    function $(id) { return document.getElementById(id); }
    function uuid() {
        if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
        return "u-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
    }
    function haversine(a, b) {
        var R = 6371008.8, toRad = Math.PI / 180;
        var dLat = (b.lat - a.lat) * toRad, dLon = (b.lon - a.lon) * toRad;
        var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(a.lat * toRad) * Math.cos(b.lat * toRad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
        return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
    }
    function localDay(ts) { var d = new Date(ts); return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate(); }
    function fmtTime(ts) {
        if (!ts) { return "–"; }
        var d = new Date(ts), now = Date.now();
        var t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        var mins = Math.round((now - ts) / 60000);
        return t + (mins >= 1 ? " (" + (mins < 60 ? mins + " min" : Math.round(mins / 60) + " h") + " ago)" : " (now)");
    }
    function toast(msg) {
        var el = $("zv-toast");
        el.textContent = msg; el.hidden = false;
        clearTimeout(toast._t);
        toast._t = setTimeout(function () { el.hidden = true; }, 3500);
    }
    function isOnline() { return navigator.onLine !== false; }

    // ------------------------------------------------------------------ state
    var cfg = Object.assign({ interval_sec: 60, min_distance_m: 25, max_accuracy_m: 150 }, boot.config || {});
    var state = {
        tracking: false, watchId: null, timer: null, last: null, lastAcc: null,
        today: { day: localDay(Date.now()), km: 0, count: 0, last: null },
        lastSync: null, syncing: false, backoff: 5000, battery: null, charging: false,
        wakeLock: null, customers: [], activeVisit: null, token: boot.token || null,
    };

    function saveKV(key, value) { return put("kv", value, key); }

    // ------------------------------------------------------------------ battery
    function initBattery() {
        if (!navigator.getBattery) { return; }
        navigator.getBattery().then(function (b) {
            function upd() { state.battery = Math.round(b.level * 100); state.charging = b.charging; render(); }
            b.addEventListener("levelchange", upd); b.addEventListener("chargingchange", upd); upd();
        }).catch(function () { /* not supported */ });
    }

    // ------------------------------------------------------------------ wake lock
    function requestWakeLock() {
        if (!state.tracking || !$("zv-wake").checked || !("wakeLock" in navigator)) { return; }
        if (state.wakeLock) { return; }
        navigator.wakeLock.request("screen").then(function (lock) {
            state.wakeLock = lock;
            lock.addEventListener("release", function () { state.wakeLock = null; });
        }).catch(function () { /* user gesture / battery saver */ });
    }
    function releaseWakeLock() { if (state.wakeLock) { state.wakeLock.release(); state.wakeLock = null; } }

    // ------------------------------------------------------------------ GPS
    function onPosition(pos) {
        var c = pos.coords;
        var p = {
            uid: uuid(),
            lat: c.latitude, lon: c.longitude,
            ts: pos.timestamp || Date.now(),
            accuracy: c.accuracy, altitude: c.altitude, speed: c.speed, heading: c.heading,
            battery: state.battery, charging: state.charging,
            offline: !isOnline(),
        };
        state.lastAcc = c.accuracy;
        if (!state.tracking) { render(); return; }
        var last = state.last;
        if (last) {
            var elapsed = p.ts - last.ts;
            var dist = haversine(last, p);
            var moved = elapsed >= cfg.interval_sec * 1000 && dist >= cfg.min_distance_m;
            var heartbeat = elapsed >= HEARTBEAT_MS;
            if (elapsed < 0 || !(moved || heartbeat)) { render(); return; }
            if (!(cfg.max_accuracy_m && c.accuracy > cfg.max_accuracy_m) && elapsed > 0 &&
                dist / (elapsed / 1000) * 3.6 < 250) {
                if (state.today.day === localDay(p.ts)) { state.today.km += dist / 1000; }
            }
        }
        recordPoint(p);
    }

    function recordPoint(p) {
        var day = localDay(p.ts);
        if (state.today.day !== day) { state.today = { day: day, km: 0, count: 0, last: null }; }
        state.today.count += 1;
        state.today.last = p.ts;
        state.last = { lat: p.lat, lon: p.lon, ts: p.ts, accuracy: p.accuracy };
        return put("points", p).then(function () {
            return Promise.all([saveKV("today", state.today), saveKV("last", state.last)]);
        }).then(function () {
            render();
            registerBackgroundSync();
            scheduleSync(800);
        });
    }

    function onGpsError(err) {
        var msg = err && err.code === 1 ? "Location permission denied - allow location for this site."
            : "Waiting for GPS…";
        $("zv-state-sub").textContent = msg;
    }

    function startWatch() {
        if (!navigator.geolocation) { toast("This browser has no GPS access"); return; }
        if (state.watchId === null) {
            state.watchId = navigator.geolocation.watchPosition(onPosition, onGpsError,
                { enableHighAccuracy: true, maximumAge: 5000, timeout: 60000 });
        }
        clearInterval(state.timer);
        // some phones throttle watchPosition: poll as a safety net
        state.timer = setInterval(function () {
            navigator.geolocation.getCurrentPosition(onPosition, function () {}, {
                enableHighAccuracy: true, maximumAge: 10000, timeout: 30000,
            });
        }, Math.max(15, cfg.interval_sec) * 1000);
        requestWakeLock();
    }
    function stopWatch() {
        if (state.watchId !== null) { navigator.geolocation.clearWatch(state.watchId); state.watchId = null; }
        clearInterval(state.timer); state.timer = null;
        releaseWakeLock();
    }

    function startDay() {
        state.tracking = true;
        saveKV("tracking", true);
        put("events", { uid: uuid(), type: "day_start", ts: Date.now() });
        startWatch();
        render(); scheduleSync(500);
        toast("Day started - tracking works even without network");
    }
    function endDay() {
        state.tracking = false;
        saveKV("tracking", false);
        put("events", { uid: uuid(), type: "day_end", ts: Date.now() });
        stopWatch();
        render(); scheduleSync(500);
        toast("Day ended");
    }

    // ------------------------------------------------------------------ sync
    function registerBackgroundSync() {
        if (!("serviceWorker" in navigator)) { return; }
        navigator.serviceWorker.ready.then(function (reg) {
            if (reg.sync && reg.sync.register) { return reg.sync.register("zivo-sync"); }
        }).catch(function () {});
    }

    function scheduleSync(delay) {
        clearTimeout(scheduleSync._t);
        scheduleSync._t = setTimeout(syncNow, delay || 0);
    }

    function syncNow() {
        if (state.syncing || !state.token) { return Promise.resolve(); }
        state.syncing = true;
        render();
        return Promise.all([getAll("points", BATCH), getAll("visits"), getAll("events")]).then(function (res) {
            var points = res[0], visits = res[1].filter(function (v) { return !v.synced; }), events = res[2];
            if (!points.length && !visits.length && !events.length) { return { done: true }; }
            return fetch(SYNC_URL, {
                method: "POST",
                credentials: "omit",
                cache: "no-store",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + state.token },
                body: JSON.stringify({ points: points, visits: visits, events: events, user_agent: navigator.userAgent }),
            }).then(function (resp) {
                if (resp.status === 401) { throw new Error("device_revoked"); }
                if (!resp.ok) { throw new Error("http_" + resp.status); }
                return resp.json();
            }).then(function (data) {
                if (data.config) { cfg = Object.assign(cfg, data.config); saveKV("config", cfg); }
                var p = data.points || {}, v = data.visits || {}, ev = data.events || {};
                var ackPoints = (p.accepted || []).concat(p.duplicates || [], (p.rejected || []).map(function (r) { return r.uid; }));
                var visitAck = (v.accepted || []).concat((v.rejected || []).map(function (r) { return r.uid; }));
                var doneVisits = [], keepVisits = [];
                visits.forEach(function (vis) {
                    if (visitAck.indexOf(vis.uid) === -1) { return; }
                    if (vis.check_out_ts) { doneVisits.push(vis.uid); } else { vis.synced = true; keepVisits.push(vis); }
                });
                return Promise.all([
                    delMany("points", ackPoints),
                    delMany("events", ev.accepted || []),
                    delMany("visits", doneVisits),
                ].concat(keepVisits.map(function (vis) { return put("visits", vis); }))).then(function () {
                    state.lastSync = Date.now(); saveKV("lastSync", state.lastSync);
                    state.backoff = 5000;
                    return { done: points.length < BATCH };
                });
            });
        }).then(function (r) {
            state.syncing = false; render();
            if (r && !r.done) { scheduleSync(200); }
        }).catch(function (err) {
            state.syncing = false;
            if (err && err.message === "device_revoked") { toast("This phone was disconnected by the administrator"); }
            state.backoff = Math.min(state.backoff * 2, 5 * 60 * 1000);
            scheduleSync(state.backoff);
            render();
        });
    }

    // ------------------------------------------------------------------ customers & visits
    function loadCustomers() {
        return get("kv", "customers").then(function (list) {
            state.customers = list || [];
            renderCustomers();
            if (!isOnline() || !state.token) { return; }
            return fetch(CUSTOMERS_URL, { headers: { "Authorization": "Bearer " + state.token }, credentials: "omit" })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (data && data.customers) {
                        state.customers = data.customers;
                        saveKV("customers", data.customers);
                        saveKV("customersAt", Date.now());
                        renderCustomers();
                    }
                }).catch(function () {});
        });
    }

    function currentFix() {
        return new Promise(function (resolve) {
            if (!navigator.geolocation) { resolve(state.last); return; }
            navigator.geolocation.getCurrentPosition(function (pos) {
                resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: pos.coords.accuracy, ts: pos.timestamp });
            }, function () { resolve(state.last); }, { enableHighAccuracy: true, maximumAge: 30000, timeout: 20000 });
        });
    }

    function checkIn(customer) {
        if (state.activeVisit) { toast("Check out of the current visit first"); return; }
        currentFix().then(function (fix) {
            var visit = {
                uid: uuid(), partner_id: customer.id, partner_name: customer.name,
                check_in_ts: Date.now(), lat: fix ? fix.lat : 0, lon: fix ? fix.lon : 0,
                accuracy: fix ? fix.accuracy : 0, offline: !isOnline(), synced: false,
            };
            state.activeVisit = visit;
            return Promise.all([put("visits", visit), saveKV("activeVisit", visit)]);
        }).then(function () {
            renderVisit(); render(); scheduleSync(300);
            toast("Checked in at " + customer.name);
        });
    }

    function checkOut() {
        var visit = state.activeVisit;
        if (!visit) { return; }
        currentFix().then(function (fix) {
            visit.check_out_ts = Date.now();
            visit.out_lat = fix ? fix.lat : 0;
            visit.out_lon = fix ? fix.lon : 0;
            visit.outcome = $("zv-outcome").value || null;
            visit.note = $("zv-note").value || "";
            visit.synced = false;
            state.activeVisit = null;
            return Promise.all([put("visits", visit), saveKV("activeVisit", null)]);
        }).then(function () {
            $("zv-outcome").value = ""; $("zv-note").value = "";
            renderVisit(); render(); scheduleSync(300);
            toast("Checked out");
        });
    }

    // ------------------------------------------------------------------ rendering
    function render() {
        var online = isOnline();
        var net = $("zv-net");
        net.textContent = online ? (state.syncing ? "Syncing…" : "Online") : "No network";
        net.className = "zv-pill " + (online ? "zv-on" : "zv-off");
        $("zv-toggle").textContent = state.tracking ? "End Day" : "Start Day";
        $("zv-toggle").className = "zv-btn " + (state.tracking ? "zv-btn-stop" : "zv-btn-start");
        $("zv-hero").className = "zv-card zv-hero" + (state.tracking ? " zv-hero-on" : "") + (state.tracking && !online ? " zv-hero-offline" : "");
        $("zv-state").textContent = state.tracking ? (online ? "Tracking live" : "Tracking offline") : "Day not started";
        $("zv-state-sub").textContent = state.tracking
            ? (online ? "Locations are uploaded automatically." : "No signal - locations are saved on this phone and will upload when the network returns.")
            : "Tap Start Day to begin tracking. Works without network.";
        $("zv-km").textContent = state.today.km.toFixed(1);
        $("zv-count").textContent = String(state.today.count);
        $("zv-acc").textContent = state.lastAcc ? Math.round(state.lastAcc) + " m" : "–";
        $("zv-lastfix").textContent = fmtTime(state.last && state.last.ts);
        $("zv-lastsync").textContent = fmtTime(state.lastSync);
        $("zv-batt").textContent = state.battery !== null ? state.battery + "%" + (state.charging ? " ⚡" : "") : "–";
        Promise.all([count("points"), getAll("visits"), count("events")]).then(function (r) {
            var pend = r[0] + r[1].filter(function (v) { return !v.synced; }).length + r[2];
            $("zv-pending").textContent = String(pend);
            $("zv-pending").parentNode.className = "zv-stat" + (pend ? " zv-stat-warn" : " zv-stat-ok");
        });
    }

    function renderCustomers() {
        var q = ($("zv-search").value || "").toLowerCase().trim();
        var list = state.customers.filter(function (c) {
            return !q || (c.name + " " + c.city + " " + c.street).toLowerCase().indexOf(q) !== -1;
        });
        if (!q) { list = list.filter(function (c) { return c.mine; }).concat(list.filter(function (c) { return !c.mine; })); }
        list = list.slice(0, 30);
        var ul = $("zv-customers");
        ul.innerHTML = "";
        list.forEach(function (c) {
            var li = document.createElement("li");
            var name = document.createElement("div"); name.className = "zv-c-name"; name.textContent = c.name;
            var sub = document.createElement("div"); sub.className = "zv-c-sub";
            var dist = "";
            if (state.last && (c.lat || c.lon)) {
                var m = haversine(state.last, c);
                dist = m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(1) + " km";
            }
            sub.textContent = [c.street, c.city, dist].filter(Boolean).join(" · ");
            var btn = document.createElement("button"); btn.type = "button"; btn.className = "zv-btn-mini"; btn.textContent = "Check in";
            btn.addEventListener("click", function () { checkIn(c); });
            var box = document.createElement("div"); box.appendChild(name); box.appendChild(sub);
            li.appendChild(box); li.appendChild(btn);
            ul.appendChild(li);
        });
        $("zv-cust-info").textContent = state.customers.length
            ? state.customers.length + " customers available offline"
            : (isOnline() ? "Loading customers…" : "Customers download when the phone is online once.");
    }

    function renderVisit() {
        var v = state.activeVisit;
        $("zv-active-visit").hidden = !v;
        $("zv-customer-picker").hidden = !!v;
        if (v) {
            $("zv-av-name").textContent = v.partner_name;
            $("zv-av-time").textContent = "Checked in " + fmtTime(v.check_in_ts) + (v.offline ? " · offline" : "");
        }
    }

    // ------------------------------------------------------------------ start-up
    function restore() {
        return Promise.all([get("kv", "tracking"), get("kv", "today"), get("kv", "last"), get("kv", "lastSync"),
            get("kv", "config"), get("kv", "activeVisit"), get("kv", "token")]).then(function (r) {
            state.tracking = !!r[0];
            if (r[1] && r[1].day === localDay(Date.now())) { state.today = r[1]; }
            state.last = r[2] || null;
            state.lastSync = r[3] || null;
            if (r[4]) { cfg = Object.assign(cfg, r[4]); }
            if (boot.config) { cfg = Object.assign(cfg, boot.config); }
            state.activeVisit = r[5] || null;
            if (boot.token) {
                state.token = boot.token;
                saveKV("token", boot.token); // also used by the service worker background sync
            } else {
                state.token = r[6] || null;
            }
        });
    }

    function bind() {
        $("zv-toggle").addEventListener("click", function () { if (state.tracking) { endDay(); } else { startDay(); } });
        $("zv-sync").addEventListener("click", function () { state.backoff = 5000; syncNow(); });
        $("zv-search").addEventListener("input", renderCustomers);
        $("zv-checkout").addEventListener("click", checkOut);
        $("zv-wake").addEventListener("change", function () { if (this.checked) { requestWakeLock(); } else { releaseWakeLock(); } });
        window.addEventListener("online", function () { render(); state.backoff = 5000; scheduleSync(300); loadCustomers(); });
        window.addEventListener("offline", render);
        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "visible") { requestWakeLock(); render(); scheduleSync(300); }
        });
        setInterval(function () { render(); if (isOnline()) { scheduleSync(0); } }, 20000);
        if ("serviceWorker" in navigator) {
            navigator.serviceWorker.addEventListener("message", function (e) {
                if (e.data && e.data.type === "zivo-synced") { state.lastSync = Date.now(); render(); }
            });
        }
    }

    function registerSW() {
        if (!("serviceWorker" in navigator)) { return; }
        navigator.serviceWorker.register("/zivoit_tracking/sw.js", { scope: "/zivoit_tracking/" }).catch(function () {});
    }

    $("zv-ver").textContent = "v" + (boot.version || "");
    registerSW();
    restore().then(function () {
        bind(); initBattery(); render(); renderVisit(); loadCustomers();
        if (state.tracking) { startWatch(); }
        scheduleSync(1000);
    });

    // test hooks (used by the automated browser test)
    window.zivoTracker = { state: state, syncNow: syncNow, onPosition: onPosition, startDay: startDay, endDay: endDay,
        count: count, getAll: getAll, cfg: function () { return cfg; } };
})();
