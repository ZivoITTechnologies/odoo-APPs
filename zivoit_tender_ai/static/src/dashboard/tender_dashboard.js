/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const EXAMPLES = [
    "ERP tenders in Kerala",
    "custom software development projects in Himachal Pradesh",
    "manpower supply for telecom in Arunachal Pradesh",
    "open tenders in railways in construction in SCR",
    "road tenders in Telangana above 5 crore closing in 15 days",
    "housekeeping and security services in Delhi",
];

export class TenderDashboard extends Component {
    static template = "zivoit_tender_ai.TenderDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.examples = EXAMPLES;
        this.state = useState({
            data: null,
            query: "",
            live: false,
            searching: false,
            result: null,
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.data = await this.orm.call("zivoit.tender", "get_dashboard_data", []);
    }

    async search(query) {
        if (query !== undefined) {
            this.state.query = query;
        }
        if (!this.state.query.trim()) {
            return;
        }
        this.state.searching = true;
        try {
            this.state.result = await this.orm.call("zivoit.tender", "agent_search", [this.state.query], {
                live: this.state.live,
                limit: 30,
            });
            if (this.state.result.live.ran) {
                this.notification.add(
                    _t("%s tenders fetched from portals", this.state.result.live.found),
                    { type: this.state.result.live.errors.length ? "warning" : "success" }
                );
                await this.load();
            }
        } finally {
            this.state.searching = false;
        }
    }

    onKeydown(ev) {
        if (ev.key === "Enter") {
            this.search();
        }
    }

    filterChips() {
        const f = this.state.result && this.state.result.filters;
        if (!f) {
            return [];
        }
        const chips = [];
        for (const z of f.zones || []) chips.push(_t("Zone: ") + z);
        for (const s of f.sector_labels || f.sectors || []) chips.push(_t("Sector: ") + s);
        for (const s of f.states || []) chips.push(_t("State: ") + s);
        for (const o of f.organisations || []) chips.push(_t("Buyer: ") + o);
        for (const k of f.keywords || []) chips.push(_t("Keyword: ") + k);
        for (const k of f.exclude_keywords || []) chips.push(_t("Not: ") + k);
        if (f.min_value) chips.push(_t("Min ₹") + (f.min_value / 1e7).toFixed(2) + " Cr");
        if (f.max_value) chips.push(_t("Max ₹") + (f.max_value / 1e7).toFixed(2) + " Cr");
        if (f.deadline_days) chips.push(_t("Closing ≤ ") + f.deadline_days + _t(" days"));
        if (f.published_days) chips.push(_t("Published ≤ ") + f.published_days + _t(" days"));
        chips.push(f.open_only === false ? _t("Incl. closed") : _t("Open only"));
        return chips;
    }

    maxCount(rows) {
        return Math.max(1, ...(rows || []).map((r) => r.count));
    }

    openTender(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivoit.tender",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openList(name, domain, context = {}) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "zivoit.tender",
            domain,
            context,
            views: [[false, "list"], [false, "kanban"], [false, "form"]],
            target: "current",
        });
    }

    openKpi(key) {
        const map = {
            open: [_t("Open Tenders"), [["is_open", "=", true]]],
            new_week: [_t("New This Week"), [["is_open", "=", true], ["is_new", "=", true]]],
            closing_week: [_t("Closing in 7 Days"), [["is_open", "=", true], ["deadline_bucket", "in", ["today", "3d", "7d"]]]],
            shortlisted: [_t("Shortlisted"), [["stage_type", "=", "shortlist"]]],
            in_preparation: [_t("Bid Preparation"), [["stage_type", "=", "prepare"]]],
            submitted: [_t("Submitted"), [["stage_type", "in", ["submitted", "evaluation"]]]],
            won: [_t("Won"), [["stage_type", "=", "won"]]],
            lost: [_t("Lost"), [["stage_type", "=", "lost"]]],
        };
        const [name, domain] = map[key];
        this.openList(name, domain);
    }

    openGroup(field, value, name) {
        const domain = [["is_open", "=", true]];
        if (field === "sector_ids") {
            domain.push(value ? ["sector_ids", "in", [value]] : ["sector_ids", "=", false]);
        } else {
            domain.push([field, "=", value || false]);
        }
        this.openList(name, domain);
    }

    openResults() {
        const ids = (this.state.result.results || []).map((r) => r.id);
        this.openList(_t("Agent: ") + this.state.query, [["id", "in", ids]]);
    }

    async saveAgent() {
        const ids = await this.orm.create("zivoit.tender.profile", [
            { name: this.state.query.slice(0, 60), nl_query: this.state.query },
        ]);
        await this.orm.call("zivoit.tender.profile", "action_parse_query", [ids]);
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivoit.tender.profile",
            res_id: ids[0],
            views: [[false, "form"]],
            target: "current",
        });
    }

    openAgents() {
        this.action.doAction("zivoit_tender_ai.action_zivoit_tender_profile");
    }
}

registry.category("actions").add("zivoit_tender_dashboard", TenderDashboard);
