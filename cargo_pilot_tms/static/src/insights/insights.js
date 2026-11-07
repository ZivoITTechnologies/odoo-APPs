import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const PERIODS = [
    { key: "month", label: _t("This month") },
    { key: "30d", label: _t("Last 30 days") },
    { key: "quarter", label: _t("This quarter") },
    { key: "year", label: _t("Financial year") },
];

/**
 * Freight Insights: what freight costs, where it goes and who performs.
 * Spend by month and mode, cost per kg and per order, channel split, lanes,
 * transporter scorecard, POD ageing and billed versus agreed.
 */
export class CpInsights extends Component {
    static template = "cargo_pilot_tms.Insights";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.state = useState({ data: null, loading: true, period: "month" });
        onWillStart(() => this.load());
    }

    async load(period) {
        if (period) {
            this.state.period = period;
        }
        this.state.loading = !this.state.data;
        this.state.data = await this.orm.call("cp.insights", "get_insights", [this.state.period]);
        this.state.loading = false;
    }

    money(value) {
        const cur = this.state.data.currency;
        const v = Math.round(value || 0);
        const n = Math.abs(v) >= 10000000 ? `${(v / 10000000).toFixed(2)} Cr`
            : Math.abs(v) >= 100000 ? `${(v / 100000).toFixed(2)} L` : v.toLocaleString();
        return cur.position === "before" ? `${cur.symbol} ${n}` : `${n} ${cur.symbol}`;
    }

    // ---- chart helpers ----
    get maxMonth() {
        return Math.max(1, ...this.state.data.months.map((m) => m.ftl + m.ltl));
    }
    barHeight(value) {
        return Math.round((value / this.maxMonth) * 150);
    }
    get modeTotal() {
        return Math.max(1, this.state.data.modes.reduce((s, m) => s + m.spend, 0));
    }
    modeShare(m) {
        return Math.round((m.spend / this.modeTotal) * 100);
    }
    get maxChannel() {
        return Math.max(1, ...this.state.data.channels.map((c) => c.spend));
    }
    get maxPod() {
        return Math.max(1, ...this.state.data.pod.map((p) => p.count));
    }
    gradeLabel(g) {
        return { a: "A", b: "B", c: "C", d: "D", new: _t("New") }[g] || g;
    }

    // ---- navigation ----
    open(model, domain, name, views = ["list", "form"], context = {}) {
        this.action.doAction({
            type: "ir.actions.act_window", name, res_model: model, domain, context,
            views: views.map((v) => [false, v]), target: "current",
        });
    }
    periodDomain(field = "planned_dispatch") {
        const d = this.state.data;
        return [[field, ">=", `${d.date_from} 00:00:00`], [field, "<=", `${d.date_to} 23:59:59`]];
    }
    openShipments(extra = [], name = _t("Shipments")) {
        this.open("cp.trip", [["state", "!=", "cancel"], ...this.periodDomain(), ...extra], name,
            ["list", "pivot", "form"]);
    }
    openOrders() {
        this.action.doAction("cargo_pilot_tms.action_cp_trip_order");
    }
    openPod() {
        this.action.doAction("cargo_pilot_tms.action_cp_trip_pod_ageing");
    }
    openScorecard() {
        this.action.doAction("cargo_pilot_tms.action_cp_scorecard");
    }
    openTransporter(id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "res.partner", res_id: id,
            views: [[false, "form"]], target: "current" });
    }
    openOffers() {
        this.action.doAction("cargo_pilot_tms.action_cp_trip_offer");
    }
    openBills() {
        this.action.doAction("cargo_pilot_tms.action_cp_freight_bill");
    }
}

registry.category("actions").add("cp_insights", CpInsights);
