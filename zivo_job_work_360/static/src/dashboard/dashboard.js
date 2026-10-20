/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const ACTIVE_STATES = ["planned", "ready", "issued", "at_vendor", "processing", "qc_pending", "completed", "hold"];

export class JobWorkDashboard extends Component {
    static template = "zivo_job_work_360.JobWorkDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ data: null, loading: true });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("zivo.job.work", "get_jw_dashboard_data", []);
        this.state.loading = false;
    }

    openJobs(domain, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: name || _t("Job Work Orders"),
            res_model: "zivo.job.work",
            views: [[false, "list"], [false, "kanban"], [false, "form"]],
            domain,
            context: {},
        });
    }

    openJob(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivo.job.work",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    openOpen() {
        this.openJobs([["state", "in", ACTIVE_STATES]], _t("Open Job Work"));
    }
    openOverdue() {
        this.openJobs([["is_overdue", "=", true]], _t("Overdue Job Work"));
    }
    openState(state, label) {
        this.openJobs([["state", "=", state]], label);
    }
    openCustody(partnerId) {
        const domain = [["balance_qty", ">", 0], ["job_id.state", "not in", ["draft", "cancel", "closed"]]];
        if (partnerId) {
            domain.push(["partner_id", "=", partnerId]);
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Material at Job Workers"),
            res_model: "zivo.job.work.material",
            views: [[false, "list"], [false, "pivot"]],
            domain,
        });
    }
    openExceptions(id) {
        if (id) {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "zivo.job.work.exception",
                res_id: id,
                views: [[false, "form"]],
            });
            return;
        }
        this.action.doAction("zivo_job_work_360.action_zivo_job_work_exception");
    }
    openScorecards() {
        this.action.doAction("zivo_job_work_360.action_zivo_job_work_vendor_score");
    }
    openNewJob() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivo.job.work",
            views: [[false, "form"]],
            target: "current",
        });
    }
    gradeClass(grade) {
        return { A: "text-bg-success", B: "text-bg-info", C: "text-bg-warning", D: "text-bg-danger" }[grade] || "text-bg-secondary";
    }
    pipelineMax() {
        return Math.max(1, ...this.state.data.pipeline.map((p) => p.count));
    }
}

registry.category("actions").add("zivo_job_work_dashboard", JobWorkDashboard);
