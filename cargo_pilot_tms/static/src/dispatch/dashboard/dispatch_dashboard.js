import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const PERIODS = [
    { key: "7d", label: _t("Last 7 days") },
    { key: "month", label: _t("This month") },
    { key: "30d", label: _t("Last 30 days") },
    { key: "quarter", label: _t("Last 3 months") },
];

/** Dispatch Dashboard (FR-10): carrier mix, bids, detention, reporting, transit and PODs. */
export class CpDispatchDashboard extends Component {
    static template = "cargo_pilot_tms.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.state = useState({ data: null, period: "month" });
        onWillStart(() => this.load());
    }

    async load(period) {
        if (period) {
            this.state.period = period;
        }
        this.state.data = await this.orm.call("cp.dispatch.dashboard", "get_data", [this.state.period]);
    }

    money(value) {
        const cur = this.state.data.currency;
        const v = Math.round(value || 0);
        const n = Math.abs(v) >= 10000000 ? `${(v / 10000000).toFixed(2)} Cr`
            : Math.abs(v) >= 100000 ? `${(v / 100000).toFixed(2)} L` : v.toLocaleString();
        return cur.position === "before" ? `${cur.symbol} ${n}` : `${n} ${cur.symbol}`;
    }

    get maxDetention() {
        return Math.max(1, ...this.state.data.detention.map((d) => d.amount));
    }
    detentionWidth(d) {
        return Math.round((d.amount / this.maxDetention) * 100);
    }
    get maxBoard() {
        return Math.max(1, ...this.state.data.board.map((b) => b.count));
    }
    boardHeight(b) {
        return Math.max(4, Math.round((b.count / this.maxBoard) * 120));
    }

    open(name, domain, res_model = "cp.dispatch.plan", views = [[false, "list"], [false, "form"]]) {
        this.action.doAction({ type: "ir.actions.act_window", name, res_model, domain, views });
    }
    openPlans(domain, title) {
        this.open(title || _t("Dispatch Plans"), domain || []);
    }
    openBoard(key) {
        const map = {
            assigned: [["state", "=", "assigned"]],
            loading: [["state", "=", "loading"]],
            in_transit: [["board_status", "=", "in_transit"]],
            delayed: [["board_status", "=", "delayed"]],
            out_for_delivery: [["board_status", "=", "out_for_delivery"]],
            delivered: [["state", "=", "delivered"]],
        };
        this.openPlans(map[key], _t("Dispatch Plans"));
    }
    openDetention() {
        this.open(_t("Detention Claims"), [], "cp.detention.record", [[false, "pivot"], [false, "list"], [false, "form"]]);
    }
    openBids() {
        this.open(_t("3PL Bids"), [["dispatch_plan_id", "!=", false]], "cp.auction");
    }
    openBills() {
        this.open(_t("Freight Bills on Hold"),
            [["cp_dispatch_id", "!=", false], ["state", "=", "draft"], ["move_type", "=", "in_invoice"]], "account.move");
    }
}

registry.category("actions").add("cp_dispatch_dashboard", CpDispatchDashboard);
