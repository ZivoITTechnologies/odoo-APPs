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
    var DB_VERSION = 2;
    var SYNC_URL = "/zivoit_tracking/api/sync";
    var CUSTOMERS_URL = "/zivoit_tracking/api/customers";
    var BATCH = 200;
    var HEARTBEAT_MS = 5 * 60 * 1000;
    var EXP_BATCH = 3; // bill photos are big: few per request

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
                    if (!d.objectStoreNames.contains("expenses")) { d.createObjectStore("expenses", { keyPath: "uid" }); }
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
        vehicle: null, dayStart: null, visitsToday: { day: localDay(Date.now()), n: 0 },
        expCat: null, expPhoto: null, expLog: [], serverToday: null, serverTodayAt: 0,
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
        state.vehicle = $("zv-vehicle").value || state.vehicle;
        saveKV("vehicle", state.vehicle);
        if (!state.dayStart || localDay(state.dayStart) !== localDay(Date.now())) {
            state.dayStart = Date.now(); saveKV("dayStart", state.dayStart);
        }
        put("events", { uid: uuid(), type: "day_start", ts: Date.now(), vehicle: state.vehicle });
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
        return Promise.all([getAll("points", BATCH), getAll("visits"), getAll("events"), getAll("expenses", EXP_BATCH)]).then(function (res) {
            var points = res[0], visits = res[1].filter(function (v) { return !v.synced; }), events = res[2];
            var expenses = res[3];
            if (!points.length && !visits.length && !events.length && !expenses.length) { return { done: true }; }
            return fetch(SYNC_URL, {
                method: "POST",
                credentials: "omit",
                cache: "no-store",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + state.token },
                body: JSON.stringify({ points: points, visits: visits, events: events, expenses: expenses,
                    user_agent: navigator.userAgent }),
            }).then(function (resp) {
                if (resp.status === 401) { throw new Error("device_revoked"); }
                if (!resp.ok) { throw new Error("http_" + resp.status); }
                return resp.json();
            }).then(function (data) {
                if (data.config) { cfg = Object.assign(cfg, data.config); saveKV("config", cfg); renderExpenseSetup(); }
                if (data.today) { state.serverToday = data.today; state.serverTodayAt = Date.now(); saveKV("serverToday", data.today); }
                var ex = data.expenses || {};
                var expOk = (ex.accepted || []).concat(ex.duplicates || []);
                var expBad = (ex.rejected || []);
                expOk.forEach(function (uid) { markExpense(uid, "ok"); });
                expBad.forEach(function (r) { markExpense(r.uid, "bad", r.reason); });
                if (expBad.length) { toast("An expense was refused by Odoo (" + expBad[0].reason + ")"); }
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
                    delMany("expenses", expOk.concat(expBad.map(function (r) { return r.uid; }))),
                    saveKV("expLog", state.expLog),
                ].concat(keepVisits.map(function (vis) { return put("visits", vis); }))).then(function () {
                    state.lastSync = Date.now(); saveKV("lastSync", state.lastSync);
                    if (expOk.length || expBad.length) { renderExpenseList(); renderExpenseHint(); }
                    state.backoff = 5000;
                    return { done: points.length < BATCH && expenses.length < EXP_BATCH };
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
            if (state.visitsToday.day !== localDay(Date.now())) { state.visitsToday = { day: localDay(Date.now()), n: 0 }; }
            state.visitsToday.n += 1;
            return Promise.all([put("visits", visit), saveKV("activeVisit", visit), saveKV("visitsToday", state.visitsToday)]);
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

    // ------------------------------------------------------------------ expenses
    function expenseCfg() { return cfg.expense || { enabled: false, categories: [], vehicles: [], da: {} }; }
    function money(v) {
        var c = expenseCfg().currency || { symbol: "", position: "before", digits: 2 };
        var n = Number(v || 0);
        var txt = n.toLocaleString(undefined, { minimumFractionDigits: n % 1 ? Math.min(2, c.digits) : 0,
            maximumFractionDigits: Math.min(2, c.digits) });
        return c.position === "after" ? txt + " " + c.symbol : c.symbol + txt;
    }
    function vehicleInfo(key) {
        var list = expenseCfg().vehicles || [];
        for (var i = 0; i < list.length; i++) { if (list[i].key === key) { return list[i]; } }
        return { key: key, label: key, rate: 0, cap_km: 0 };
    }
    function categoryById(id) {
        var list = expenseCfg().categories || [];
        for (var i = 0; i < list.length; i++) { if (list[i].id === id) { return list[i]; } }
        return null;
    }
    function todayLog() {
        var today = localDay(Date.now());
        return state.expLog.filter(function (e) { return localDay(e.ts) === today; });
    }

    function estimateToday() {
        var ex = expenseCfg(), da = ex.da || {};
        var km = state.today.day === localDay(Date.now()) ? state.today.km : 0;
        var v = vehicleInfo(state.vehicle || ex.vehicle);
        var paidKm = v.cap_km ? Math.min(km, v.cap_km) : km;
        var ta = Math.round(paidKm * (v.rate || 0) * 100) / 100;
        var hours = state.dayStart && localDay(state.dayStart) === localDay(Date.now())
            ? ((state.tracking ? Date.now() : (state.today.last || Date.now())) - state.dayStart) / 3600000 : 0;
        var visits = state.visitsToday.day === localDay(Date.now()) ? state.visitsToday.n : 0;
        var daAmt = 0, kind = "";
        if (!(da.min_visits && visits < da.min_visits)) {
            if (da.outstation_km && km > da.outstation_km && da.outstation && hours >= (da.half_hours || 0)) { daAmt = da.outstation; kind = "outstation"; }
            else if (da.full && hours >= da.full_hours) { daAmt = da.full; kind = "full day"; }
            else if (da.half && hours >= da.half_hours) { daAmt = da.half; kind = "half day"; }
        }
        var bills = todayLog().filter(function (e) { return e.status !== "bad"; })
            .reduce(function (a, e) { return a + Number(e.amount || 0); }, 0);
        return { km: km, ta: ta, da: daAmt, daKind: kind, bills: bills, total: ta + daAmt + bills, vehicle: v };
    }

    function renderAllowance() {
        var ex = expenseCfg();
        $("zv-allow-card").hidden = !ex.enabled;
        $("zv-exp-card").hidden = !ex.enabled || !(ex.categories || []).length;
        if (!ex.enabled) { return; }
        var est = estimateToday();
        var srv = state.serverToday;
        var useServer = srv && srv.date && isOnline() && Date.now() - state.serverTodayAt < 10 * 60 * 1000 &&
            !state.tracking && Number(srv.km || 0) >= est.km - 0.05;
        var ta = useServer ? srv.ta : est.ta, da = useServer ? srv.da : est.da;
        $("zv-ta").textContent = money(ta);
        $("zv-ta-lbl").textContent = est.vehicle.rate ? "Mileage " + est.km.toFixed(1) + " km" : "Mileage";
        $("zv-da").textContent = money(da);
        $("zv-da-lbl").textContent = est.daKind ? "DA " + est.daKind : "Daily allow.";
        $("zv-bills").textContent = money(est.bills);
        $("zv-total").textContent = money(Number(ta) + Number(da) + est.bills);
        $("zv-allow-src").textContent = useServer ? "calculated by Odoo" : "estimate";
    }

    function renderExpenseSetup() {
        var ex = expenseCfg();
        var sel = $("zv-vehicle");
        sel.innerHTML = "";
        (ex.vehicles || []).forEach(function (v) {
            var o = document.createElement("option");
            o.value = v.key;
            o.textContent = v.label + (v.rate ? " · " + money(v.rate) + "/km" : "");
            sel.appendChild(o);
        });
        sel.value = state.vehicle || ex.vehicle || "two_wheeler";
        var box = $("zv-exp-cats");
        box.innerHTML = "";
        (ex.categories || []).forEach(function (c) {
            var b = document.createElement("button");
            b.type = "button";
            b.className = "zv-chip" + (state.expCat === c.id ? " zv-chip-on" : "");
            b.textContent = (c.icon ? c.icon + " " : "") + c.name;
            b.setAttribute("data-cat", String(c.id));
            b.addEventListener("click", function () { state.expCat = c.id; renderExpenseSetup(); renderExpenseHint(); });
            box.appendChild(b);
        });
        renderExpenseHint();
        renderExpenseList();
        render();
    }

    function receiptNeeded(cat, amount) {
        if (!cat || cat.receipt === "never") { return false; }
        if (cat.receipt === "always") { return true; }
        return amount > (cat.receipt_above || 0);
    }

    function renderExpenseHint() {
        var cat = categoryById(state.expCat);
        var amount = parseFloat($("zv-exp-amount").value || "0");
        var hint = "";
        if (!cat) { hint = "Choose a category."; }
        else {
            if (receiptNeeded(cat, amount) && !state.expPhoto) { hint = "Bill photo required for " + cat.name + (cat.receipt === "above" ? " above " + money(cat.receipt_above) : "") + "."; }
            if (cat.daily_limit) {
                var spent = todayLog().filter(function (e) { return e.category_id === cat.id && e.status !== "bad"; })
                    .reduce(function (a, e) { return a + Number(e.amount || 0); }, 0);
                hint += (hint ? " " : "") + "Daily limit " + money(cat.daily_limit) + " (used " + money(spent) + ").";
            }
        }
        $("zv-exp-hint").textContent = hint;
        $("zv-exp-photo-btn").className = "zv-photo" + (state.expPhoto ? " zv-photo-ok" : "");
        $("zv-exp-photo-lbl").textContent = state.expPhoto ? "✓ Photo added" : "📷 Bill photo";
    }

    function renderExpenseList() {
        var ul = $("zv-exp-list");
        ul.innerHTML = "";
        todayLog().slice().reverse().forEach(function (e) {
            var li = document.createElement("li");
            var box = document.createElement("div");
            var name = document.createElement("div"); name.className = "zv-c-name";
            name.textContent = e.name;
            var badge = document.createElement("span");
            badge.className = "zv-badge " + (e.status === "ok" ? "zv-badge-ok" : (e.status === "bad" ? "zv-badge-bad" : "zv-badge-wait"));
            badge.textContent = e.status === "ok" ? "uploaded" : (e.status === "bad" ? "refused" : "waiting upload");
            name.appendChild(badge);
            var sub = document.createElement("div"); sub.className = "zv-c-sub";
            sub.textContent = new Date(e.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
                (e.photo ? " · 📷 bill" : "") + (e.partner ? " · " + e.partner : "") + (e.reason ? " · " + e.reason : "");
            box.appendChild(name); box.appendChild(sub);
            var amt = document.createElement("div"); amt.className = "zv-exp-amt"; amt.textContent = money(e.amount);
            li.appendChild(box); li.appendChild(amt);
            ul.appendChild(li);
        });
    }

    function markExpense(uid, status, reason) {
        state.expLog.forEach(function (e) { if (e.uid === uid) { e.status = status; e.reason = reason || ""; } });
    }

    function compressImage(file) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onerror = function () { reject(reader.error); };
            reader.onload = function () {
                var img = new Image();
                img.onerror = function () { reject(new Error("not_an_image")); };
                img.onload = function () {
                    function draw(max, quality) {
                        var scale = Math.min(1, max / Math.max(img.width, img.height));
                        var c = document.createElement("canvas");
                        c.width = Math.max(1, Math.round(img.width * scale));
                        c.height = Math.max(1, Math.round(img.height * scale));
                        var ctx = c.getContext("2d");
                        ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
                        ctx.drawImage(img, 0, 0, c.width, c.height);
                        return c.toDataURL("image/jpeg", quality);
                    }
                    var out = draw(1280, 0.72);
                    if (out.length > 900000) { out = draw(1024, 0.55); }
                    resolve(out);
                };
                img.src = reader.result;
            };
            reader.readAsDataURL(file);
        });
    }

    function saveExpense() {
        var cat = categoryById(state.expCat);
        var amount = parseFloat($("zv-exp-amount").value || "0");
        if (!cat) { toast("Choose a category"); return; }
        if (!(amount > 0)) { toast("Enter the amount"); return; }
        if (receiptNeeded(cat, amount) && !state.expPhoto) { toast("Add a photo of the bill"); renderExpenseHint(); return; }
        $("zv-exp-save").disabled = true;
        currentFix().then(function (fix) {
            var note = ($("zv-exp-note").value || "").trim();
            var item = {
                uid: uuid(), category_id: cat.id, amount: amount, note: note, ts: Date.now(),
                lat: fix ? fix.lat : 0, lon: fix ? fix.lon : 0, accuracy: fix ? fix.accuracy : 0,
                offline: !isOnline(), receipt: state.expPhoto || null,
                partner_id: state.activeVisit ? state.activeVisit.partner_id : null,
            };
            state.expLog.push({ uid: item.uid, name: note || cat.name, category_id: cat.id, amount: amount, ts: item.ts,
                photo: !!item.receipt, partner: state.activeVisit ? state.activeVisit.partner_name : "", status: "wait" });
            // keep two weeks of history on the phone
            var limit = Date.now() - 14 * 86400000;
            state.expLog = state.expLog.filter(function (e) { return e.ts > limit; });
            return Promise.all([put("expenses", item), saveKV("expLog", state.expLog)]);
        }).then(function () {
            $("zv-exp-amount").value = ""; $("zv-exp-note").value = ""; $("zv-exp-photo").value = "";
            state.expPhoto = null; $("zv-exp-preview").hidden = true; $("zv-exp-preview").removeAttribute("src");
            renderExpenseHint(); renderExpenseList(); render(); scheduleSync(300);
            toast(isOnline() ? "Expense saved" : "Expense saved on the phone - uploads when online");
        }).catch(function () { toast("Could not save the expense"); }).then(function () { $("zv-exp-save").disabled = false; });
    }

    function onPhoto() {
        var file = this.files && this.files[0];
        if (!file) { return; }
        $("zv-exp-photo-lbl").textContent = "Processing…";
        compressImage(file).then(function (dataUrl) {
            state.expPhoto = dataUrl;
            $("zv-exp-preview").src = dataUrl; $("zv-exp-preview").hidden = false;
            renderExpenseHint();
        }).catch(function () { state.expPhoto = null; toast("This file is not a photo"); renderExpenseHint(); });
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
        $("zv-vehicle-box").hidden = state.tracking || !expenseCfg().enabled;
        renderAllowance();
        Promise.all([count("points"), getAll("visits"), count("events"), count("expenses")]).then(function (r) {
            var pend = r[0] + r[1].filter(function (v) { return !v.synced; }).length + r[2] + r[3];
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
            get("kv", "config"), get("kv", "activeVisit"), get("kv", "token"), get("kv", "vehicle"), get("kv", "dayStart"),
            get("kv", "visitsToday"), get("kv", "expLog"), get("kv", "serverToday")]).then(function (r) {
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
            state.vehicle = r[7] || (cfg.expense && cfg.expense.vehicle) || "two_wheeler";
            state.dayStart = r[8] || null;
            if (r[9] && r[9].day === localDay(Date.now())) { state.visitsToday = r[9]; }
            state.expLog = r[10] || [];
            state.serverToday = r[11] || null;
        });
    }

    function bind() {
        $("zv-toggle").addEventListener("click", function () { if (state.tracking) { endDay(); } else { startDay(); } });
        $("zv-sync").addEventListener("click", function () { state.backoff = 5000; syncNow(); });
        $("zv-search").addEventListener("input", renderCustomers);
        $("zv-checkout").addEventListener("click", checkOut);
        $("zv-wake").addEventListener("change", function () { if (this.checked) { requestWakeLock(); } else { releaseWakeLock(); } });
        $("zv-vehicle").addEventListener("change", function () { state.vehicle = this.value; saveKV("vehicle", state.vehicle); render(); });
        $("zv-exp-photo").addEventListener("change", onPhoto);
        $("zv-exp-amount").addEventListener("input", renderExpenseHint);
        $("zv-exp-save").addEventListener("click", saveExpense);
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
        bind(); initBattery(); renderExpenseSetup(); render(); renderVisit(); loadCustomers();
        if (state.tracking) { startWatch(); }
        scheduleSync(1000);
    });

    // test hooks (used by the automated browser test)
    window.zivoTracker = { state: state, syncNow: syncNow, onPosition: onPosition, startDay: startDay, endDay: endDay,
        count: count, getAll: getAll, cfg: function () { return cfg; }, estimateToday: estimateToday,
        setPhoto: function (dataUrl) { state.expPhoto = dataUrl; renderExpenseHint(); } };
})();
