/* global L */

import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const REFRESH_MS = 60000;
const STATUS_COLORS = { ok: "#2e7d6b", late: "#b5525b", stopped: "#c9783b" };

export class CpControlTower extends Component {
    static template = "cargo_pilot_tms.ControlTower";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ data: null, loading: true, showSetup: true });
        this.mapRef = useRef("map");
        this.map = null;
        this.layer = null;
        onWillStart(() => this.load());
        onMounted(() => this.renderMap());
        this.timer = setInterval(() => this.load(true), REFRESH_MS);
        onWillUnmount(() => {
            clearInterval(this.timer);
            if (this.map) {
                this.map.remove();
            }
        });
    }

    async load(redraw = false) {
        this.state.data = await this.orm.call("cp.dashboard", "get_dashboard_data", []);
        this.state.loading = false;
        if (redraw) {
            this.renderMap();
        }
    }

    // ------------------------------------------------------------------
    // Map
    // ------------------------------------------------------------------
    renderMap() {
        const el = this.mapRef.el;
        if (!el || typeof L === "undefined") {
            return;
        }
        if (!this.map) {
            this.map = L.map(el, { zoomControl: true, attributionControl: true, scrollWheelZoom: false });
            L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 18,
                attribution: "&copy; OpenStreetMap contributors",
            }).addTo(this.map);
            this.layer = L.layerGroup().addTo(this.map);
        }
        this.layer.clearLayers();
        const bounds = [];
        for (const p of this.state.data.positions) {
            const color = STATUS_COLORS[p.status] || STATUS_COLORS.ok;
            if (p.origin && p.dest) {
                L.polyline([p.origin, [p.lat, p.lon], p.dest], {
                    color, weight: 2, opacity: 0.45, dashArray: "6 6",
                }).addTo(this.layer);
                L.circleMarker(p.dest, { radius: 4, color, weight: 2, fillColor: "#fff", fillOpacity: 1 })
                    .addTo(this.layer);
                bounds.push(p.dest);
            }
            const marker = L.circleMarker([p.lat, p.lon], {
                radius: 9, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1,
            }).addTo(this.layer);
            const eta = p.eta ? `<br/>ETA ${p.eta}` : "";
            marker.bindTooltip(`<b>${p.vehicle || p.name}</b><br/>${p.lane}${eta}`, { direction: "top" });
            marker.on("click", () => this.openRecord("cp.trip", p.id));
            bounds.push([p.lat, p.lon]);
        }
        if (bounds.length) {
            this.map.fitBounds(bounds, { padding: [30, 30], maxZoom: 9 });
        } else {
            this.map.setView([21.0, 78.9], 5);
        }
        setTimeout(() => this.map && this.map.invalidateSize(), 50);
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    money(value) {
        const cur = this.state.data.currency;
        const n = Math.round(value || 0).toLocaleString();
        return cur.position === "before" ? `${cur.symbol} ${n}` : `${n} ${cur.symbol}`;
    }

    number(value) {
        return Math.round(value || 0).toLocaleString();
    }

    get maxDaily() {
        return Math.max(1, ...this.state.data.daily.map((d) => d.value));
    }

    get maxStage() {
        return Math.max(1, ...this.state.data.pipeline.map((s) => s.count));
    }

    get setupProgress() {
        const steps = this.state.data.setup;
        return Math.round((steps.filter((s) => s.done).length / steps.length) * 100);
    }

    get openIssueTotal() {
        const c = this.state.data.issue_counts;
        return c.critical + c.high + c.medium + c.low;
    }

    severityLabel(sev) {
        return { critical: _t("Critical"), high: _t("High"), medium: _t("Medium"), low: _t("Low") }[sev] || sev;
    }

    // ------------------------------------------------------------------
    // Navigation
    // ------------------------------------------------------------------
    open(model, domain, name, views = ["list", "form"]) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: model,
            domain,
            views: views.map((v) => [false, v]),
            target: "current",
        });
    }

    openRecord(model, id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openXml(xmlId) {
        this.action.doAction(xmlId);
    }

    async bookLoad() {
        await this.action.doAction("cargo_pilot_tms.action_cp_booking", {
            onClose: () => this.load(true),
        });
    }

    openTrips(domain, name) {
        this.open("cp.trip", domain, name || _t("Shipments"), ["kanban", "list", "form"]);
    }

    onTile(key) {
        const moving = ["in_transit", "arrived"];
        const map = {
            open_indents: () => this.open("cp.indent", [["state", "in", ["confirmed", "sourcing"]]],
                _t("Open Load Requests"), ["kanban", "list", "form"]),
            live_auctions: () => this.open("cp.auction", [["state", "in", ["scheduled", "live"]]],
                _t("Live Bidding"), ["kanban", "list", "form"]),
            in_transit: () => this.openTrips([["state", "in", moving]], _t("On the Road")),
            late: () => this.openTrips([["state", "in", moving], ["is_late", "=", true]], _t("Running Late")),
            epod_pending: () => this.openTrips(
                [["state", "in", ["arrived", "delivered"]], ["epod_state", "in", ["none", "pending", "exception"]]],
                _t("Waiting for Delivery Proof")),
            bills_in_audit: () => this.open("cp.freight.bill", [["state", "in", ["submitted", "audited"]]],
                _t("Invoices to Check"), ["kanban", "list", "form"]),
            issues: () => this.openXml("cargo_pilot_tms.action_cp_exception"),
        };
        if (map[key]) {
            map[key]();
        }
    }

    onStage(key, label) {
        this.openTrips([["state", "=", key]], label);
    }

    async resolveIssue(id) {
        await this.orm.call("cp.exception", "action_resolve", [[id]]);
        await this.load();
    }
}

registry.category("actions").add("cp_control_tower", CpControlTower);
