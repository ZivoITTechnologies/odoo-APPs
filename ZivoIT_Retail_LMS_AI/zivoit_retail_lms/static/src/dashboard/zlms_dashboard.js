import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class ZlmsDashboard extends Component {
    static template = "zivoit_retail_lms.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ data: null, region: false, store: false, loading: true, thinking: false });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("zlms.dashboard", "get_dashboard_data", [], {
            region_id: this.state.region || false,
            store_id: this.state.store || false,
        });
        this.state.loading = false;
    }

    async onRegionChange(ev) {
        this.state.region = parseInt(ev.target.value) || false;
        this.state.store = false;
        await this.load();
    }

    async onStoreChange(ev) {
        this.state.store = parseInt(ev.target.value) || false;
        await this.load();
    }

    get storeDomain() {
        const ids = (this.state.data?.stores || []).map((s) => s.id);
        return [["store_id", "in", ids]];
    }

    openList(model, name, domain = []) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: model,
            domain,
            views: [[false, "list"], [false, "form"]],
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

    openOverdue() {
        this.openList("zlms.assignment", "Overdue Assignments", [...this.storeDomain, ["is_overdue", "=", true]]);
    }
    openOpen() {
        this.openList("zlms.assignment", "Open Assignments", [
            ...this.storeDomain,
            ["state", "in", ["assigned", "in_progress"]],
        ]);
    }
    openLearners() {
        this.openList("zlms.learner", "Learners", this.storeDomain);
    }
    openAtRisk() {
        this.openList("zlms.learner", "At-risk Learners", [...this.storeDomain, ["risk_level", "=", "high"]]);
    }
    openCerts() {
        this.openList("zlms.certificate", "Certificates", [...this.storeDomain, ["state", "in", ["valid", "expiring"]]]);
    }
    openExpiring() {
        this.openList("zlms.certificate", "Expiring Certificates", [...this.storeDomain, ["state", "=", "expiring"]]);
    }
    openSessions() {
        this.openList("zlms.roleplay.session", "Role-play Sessions", this.storeDomain);
    }
    openBulletins() {
        this.openList("zlms.bulletin.ack", "Bulletin Acknowledgements", [
            ...this.storeDomain,
            ["bulletin_id.state", "=", "published"],
            ["state", "!=", "acknowledged"],
        ]);
    }
    openSkills() {
        this.action.doAction("zivoit_retail_lms.zlms_learner_skill_action");
    }
    openPromotion() {
        this.openList("zlms.learner", "Ready for Promotion", [...this.storeDomain, ["promotion_ready", "=", true]]);
    }
    openObservations() {
        this.openList("zlms.observation", "Coaching Observations", this.storeDomain);
    }
    openSessions2() {
        this.action.doAction("zivoit_retail_lms.zlms_training_session_action");
    }
    openPath(id) {
        this.openList("zlms.assignment", "Assignments", [...this.storeDomain, ["path_id", "=", id]]);
    }

    async networkInsight() {
        this.state.thinking = true;
        try {
            const action = await this.orm.call("zlms.dashboard", "action_network_insight", []);
            await this.action.doAction(action);
        } finally {
            this.state.thinking = false;
        }
    }

    barClass(value) {
        if (value >= 80) {
            return "bg-success";
        }
        if (value >= 50) {
            return "bg-warning";
        }
        return "bg-danger";
    }
}

registry.category("actions").add("zlms_dashboard", ZlmsDashboard);
