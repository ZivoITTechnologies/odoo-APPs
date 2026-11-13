/** @odoo-module **/
/* ZivoIT Offline Field Sales Tracking - manager live map & route replay
 * Copyright 2026 ZivoIT Technologies - License OPL-1 */
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

const STATUS = {
    live: { label: _t("Live"), color: "#12a150" },
    delayed: { label: _t("Delayed"), color: "#f08c00" },
    nosignal: { label: _t("No signal"), color: "#d6336c" },
    ended: { label: _t("Day ended"), color: "#5c6b80" },
    notstarted: { label: _t("Not started"), color: "#9aa7b8" },
};
const REFRESH_MS = 30000;
const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const DEFAULT_CENTER = [17.385, 78.4867];

export class ZivoLiveMap extends Component {
    static template = "zivoit_field_tracking.LiveMap";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.mapRef = useRef("map");
        this.STATUS = STATUS;
        this.state = useState({
            mode: "live",
            users: [],
            filter: "all",
            search: "",
            selectedUser: null,
            route: null,
            replayIndex: 0,
            playing: false,
            loading: true,
            tilesFailed: false,
            serverTime: null,
            isManager: false,
            routeDate: luxon.DateTime.local().toISODate(),
            routeUserId: null,
            message: "",
        });
        this.markers = {};
        this.routeLayer = null;
        this.tileErrors = 0;

        onWillStart(async () => {
            await this.loadBoard();
        });
        onMounted(async () => {
            this.initMap();
            this.drawLive(true);
            const sessionId = this.props.action?.params?.session_id;
            if (sessionId) {
                await this.openRoute(sessionId);
            }
            this.timer = setInterval(() => this.refresh(), REFRESH_MS);
        });
        onWillUnmount(() => {
            clearInterval(this.timer);
            clearInterval(this.playTimer);
            if (this.map) {
                this.map.remove();
            }
        });
    }

    // ------------------------------------------------------------------ data
    async loadBoard() {
        const data = await this.orm.call("zivo.track.point", "get_live_board", []);
        this.state.users = data.users;
        this.state.serverTime = data.server_time;
        this.state.isManager = data.is_manager;
        this.tileUrl = data.tile_url;
        this.tileAttribution = data.tile_attribution;
        this.state.loading = false;
        if (!this.state.routeUserId && data.users.length) {
            this.state.routeUserId = data.users[0].user_id;
        }
    }

    async refresh() {
        if (this.state.mode !== "live") {
            return;
        }
        await this.loadBoard();
        this.drawLive(false);
    }

    get counts() {
        const counts = { all: this.state.users.length, live: 0, delayed: 0, nosignal: 0, ended: 0, notstarted: 0 };
        for (const u of this.state.users) {
            counts[u.status] += 1;
        }
        return counts;
    }

    get visibleUsers() {
        const q = this.state.search.trim().toLowerCase();
        return this.state.users.filter(
            (u) => (this.state.filter === "all" || u.status === this.state.filter) &&
                (!q || u.name.toLowerCase().includes(q))
        );
    }

    formatTime(value) {
        if (!value) {
            return "–";
        }
        return formatDateTime(deserializeDateTime(value), { format: "dd MMM HH:mm" });
    }

    ago(value) {
        if (!value || !this.state.serverTime) {
            return "";
        }
        const mins = Math.round(
            deserializeDateTime(this.state.serverTime).diff(deserializeDateTime(value), "minutes").minutes
        );
        if (mins < 1) {
            return _t("just now");
        }
        if (mins < 60) {
            return _t("%s min ago", mins);
        }
        if (mins < 1440) {
            return _t("%s h ago", Math.round(mins / 60));
        }
        return _t("%s d ago", Math.round(mins / 1440));
    }

    // ------------------------------------------------------------------ map
    initMap() {
        this.map = L.map(this.mapRef.el, { zoomControl: true, attributionControl: true }).setView(DEFAULT_CENTER, 11);
        const tiles = L.tileLayer(this.tileUrl || "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: this.tileAttribution || "",
        });
        tiles.on("tileerror", () => {
            this.tileErrors += 1;
            if (this.tileErrors > 3) {
                this.state.tilesFailed = true;
            }
        });
        tiles.on("tileload", () => {
            this.tileErrors = 0;
            this.state.tilesFailed = false;
        });
        tiles.addTo(this.map);
        this.liveLayer = L.layerGroup().addTo(this.map);
        setTimeout(() => this.map && this.map.invalidateSize(), 200);
    }

    userIcon(user) {
        const color = STATUS[user.status].color;
        const initials = user.name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
        return L.divIcon({
            className: "zv-marker",
            html: '<div class="zv-marker-pin" style="background:' + color + '"><span>' + esc(initials) + "</span></div>",
            iconSize: [36, 44],
            iconAnchor: [18, 42],
            popupAnchor: [0, -38],
        });
    }

    popupHtml(u) {
        const lines = [
            "<b>" + esc(u.name) + "</b>",
            '<span style="color:' + STATUS[u.status].color + '">● ' + esc(STATUS[u.status].label) + "</span> · " + esc(this.ago(u.fix_time)),
            esc(_t("GPS time")) + ": " + esc(this.formatTime(u.fix_time)),
            esc(_t("Today")) + ": " + u.distance_km.toFixed(1) + " km · " + u.point_count + " " + esc(_t("points")),
        ];
        if (u.offline_points) {
            lines.push('<span class="zv-off">' + u.offline_points + " " + esc(_t("points captured offline")) + "</span>");
        }
        if (u.battery) {
            lines.push(esc(_t("Battery")) + ": " + Math.round(u.battery) + "%");
        }
        if (u.is_mock) {
            lines.push('<b style="color:#d6336c">' + esc(_t("Mock GPS detected")) + "</b>");
        }
        return '<div class="zv-pop">' + lines.join("<br/>") + "</div>";
    }

    drawLive(fit) {
        if (!this.map) {
            return;
        }
        this.liveLayer.clearLayers();
        this.markers = {};
        const bounds = [];
        for (const u of this.state.users) {
            if (!u.lat && !u.lon) {
                continue;
            }
            if (u.trail && u.trail.length > 1 && u.status !== "notstarted") {
                L.polyline(u.trail, { color: STATUS[u.status].color, weight: 3, opacity: 0.55 }).addTo(this.liveLayer);
                u.trail.forEach((pt) => bounds.push(pt));
            }
            const m = L.marker([u.lat, u.lon], { icon: this.userIcon(u), title: u.name })
                .bindPopup(this.popupHtml(u))
                .addTo(this.liveLayer);
            if (u.accuracy && u.accuracy < 2000) {
                L.circle([u.lat, u.lon], {
                    radius: u.accuracy, color: STATUS[u.status].color, weight: 1, fillOpacity: 0.08,
                }).addTo(this.liveLayer);
            }
            this.markers[u.user_id] = m;
            bounds.push([u.lat, u.lon]);
        }
        if (fit && bounds.length) {
            this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 15 });
        }
    }

    focusUser(u) {
        this.state.selectedUser = u.user_id;
        this.state.routeUserId = u.user_id;
        const m = this.markers[u.user_id];
        if (m) {
            this.map.setView(m.getLatLng(), Math.max(this.map.getZoom(), 14));
            m.openPopup();
        }
    }

    setFilter(f) {
        this.state.filter = f;
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    // ------------------------------------------------------------------ route replay
    async openTodayRoute(u) {
        if (u.session_id) {
            await this.openRoute(u.session_id);
        } else {
            this.state.message = _t("No location recorded today for %s", u.name);
        }
    }

    async loadRouteByDate() {
        if (!this.state.routeUserId || !this.state.routeDate) {
            return;
        }
        const ids = await this.orm.search("zivo.track.session", [
            ["user_id", "=", parseInt(this.state.routeUserId)],
            ["date", "=", this.state.routeDate],
        ], { limit: 1 });
        if (!ids.length) {
            this.state.message = _t("No day log for this salesperson on that date.");
            return;
        }
        await this.openRoute(ids[0]);
    }

    async openRoute(sessionId) {
        const data = await this.orm.call("zivo.track.session", "get_route_data", [[sessionId]]);
        this.state.mode = "route";
        this.state.route = data;
        this.state.replayIndex = data.points.length ? data.points.length - 1 : 0;
        this.state.message = "";
        this.drawRoute();
    }

    backToLive() {
        this.stopReplay();
        this.state.mode = "live";
        this.state.route = null;
        if (this.routeLayer) {
            this.routeLayer.remove();
            this.routeLayer = null;
        }
        this.liveLayer.addTo(this.map);
        this.refresh().then(() => this.drawLive(true));
    }

    drawRoute() {
        const route = this.state.route;
        this.liveLayer.remove();
        if (this.routeLayer) {
            this.routeLayer.remove();
        }
        this.routeLayer = L.layerGroup().addTo(this.map);
        const pts = route.points;
        if (!pts.length) {
            return;
        }
        // split into online / offline segments so the manager sees what was buffered on the phone
        let segment = [[pts[0].lat, pts[0].lon]];
        let offline = pts[0].offline;
        const flush = () => {
            if (segment.length > 1) {
                L.polyline(segment, offline
                    ? { color: "#f08c00", weight: 5, opacity: 0.95, dashArray: "8 7" }
                    : { color: "#1f6feb", weight: 5, opacity: 0.9 }).addTo(this.routeLayer);
            }
        };
        for (let i = 1; i < pts.length; i++) {
            const p = pts[i];
            segment.push([p.lat, p.lon]);
            if (p.offline !== offline) {
                flush();
                segment = [[p.lat, p.lon]];
                offline = p.offline;
            }
        }
        flush();
        for (const s of route.stops) {
            L.circleMarker([s.lat, s.lon], { radius: 9, color: "#6741d9", weight: 2, fillColor: "#b197fc", fillOpacity: 0.8 })
                .bindTooltip(esc(_t("Stop")) + " " + Math.round(s.minutes) + " min · " + esc(this.formatTime(s.start)))
                .addTo(this.routeLayer);
        }
        for (const v of route.visits) {
            const ok = v.within_geofence;
            L.marker([v.lat, v.lon], {
                icon: L.divIcon({
                    className: "zv-marker",
                    html: '<div class="zv-visit ' + (ok ? "zv-visit-ok" : "zv-visit-bad") + '"><i class="fa fa-briefcase"></i></div>',
                    iconSize: [28, 28], iconAnchor: [14, 14],
                }),
            }).bindTooltip(esc(v.partner) + " · " + esc(this.formatTime(v.check_in)) + (ok ? "" : " · " + esc(_t("outside geo-fence"))))
                .addTo(this.routeLayer);
        }
        const first = pts[0], last = pts[pts.length - 1];
        L.circleMarker([first.lat, first.lon], { radius: 8, color: "#fff", weight: 3, fillColor: "#12a150", fillOpacity: 1 })
            .bindTooltip(esc(_t("Start")) + " " + esc(this.formatTime(first.time))).addTo(this.routeLayer);
        L.circleMarker([last.lat, last.lon], { radius: 8, color: "#fff", weight: 3, fillColor: "#d6336c", fillOpacity: 1 })
            .bindTooltip(esc(_t("Last")) + " " + esc(this.formatTime(last.time))).addTo(this.routeLayer);
        this.replayMarker = L.circleMarker([last.lat, last.lon], {
            radius: 10, color: "#0b1f3a", weight: 3, fillColor: "#ffd43b", fillOpacity: 1,
        }).addTo(this.routeLayer);
        this.map.fitBounds(pts.map((p) => [p.lat, p.lon]), { padding: [50, 50], maxZoom: 16 });
    }

    get replayPoint() {
        const route = this.state.route;
        return route && route.points.length ? route.points[this.state.replayIndex] : null;
    }

    onReplayInput(ev) {
        this.state.replayIndex = parseInt(ev.target.value);
        this.moveReplay();
    }

    moveReplay() {
        const p = this.replayPoint;
        if (p && this.replayMarker) {
            this.replayMarker.setLatLng([p.lat, p.lon]);
        }
    }

    toggleReplay() {
        if (this.state.playing) {
            this.stopReplay();
            return;
        }
        const n = this.state.route.points.length;
        if (this.state.replayIndex >= n - 1) {
            this.state.replayIndex = 0;
        }
        this.state.playing = true;
        const step = Math.max(1, Math.round(n / 300));
        this.playTimer = setInterval(() => {
            if (this.state.replayIndex >= n - 1) {
                this.stopReplay();
                return;
            }
            this.state.replayIndex = Math.min(n - 1, this.state.replayIndex + step);
            this.moveReplay();
        }, 60);
    }

    stopReplay() {
        this.state.playing = false;
        clearInterval(this.playTimer);
    }

    openSession() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivo.track.session",
            res_id: this.state.route.session.id,
            views: [[false, "form"]],
        });
    }
}

registry.category("actions").add("zivo_track_live_map", ZivoLiveMap);
