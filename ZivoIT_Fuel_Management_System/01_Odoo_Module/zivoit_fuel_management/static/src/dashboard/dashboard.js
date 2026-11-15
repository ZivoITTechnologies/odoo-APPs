/** @odoo-module **/
import { Component, onWillStart, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";

const { DateTime } = luxon;

export class BpclPumpDashboard extends Component {
    static template = "zivoit_fuel_management.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.chartRef = useRef("chart");
        this.chart = null;
        this.state = useState({ data: null, date: DateTime.now().toISODate(), loading: true,
                                aiQuestion: "", aiAnswer: "", aiBusy: false });
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            await this.load();
        });
        onMounted(() => this.renderChart());
        onWillUnmount(() => this.chart && this.chart.destroy());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("pump.dashboard", "get_dashboard_data", [], { date: this.state.date });
        this.state.loading = false;
    }

    async reload() {
        await this.load();
        this.renderChart();
    }

    async shiftDate(days) {
        this.state.date = DateTime.fromISO(this.state.date).plus({ days }).toISODate();
        await this.reload();
    }

    async onDateChange(ev) {
        if (ev.target.value) {
            this.state.date = ev.target.value;
            await this.reload();
        }
    }

    async goToday() {
        this.state.date = DateTime.now().toISODate();
        await this.reload();
    }

    get displayDate() {
        return DateTime.fromISO(this.state.date).toFormat("dd LLL yyyy");
    }

    fmt(value, digits = 2) {
        const v = Number(value || 0);
        return v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    }

    money(value) {
        const cur = (this.state.data && this.state.data.currency) || "₹";
        return `${cur} ${this.fmt(value)}`;
    }

    compact(value) {
        const v = Number(value || 0);
        const abs = Math.abs(v);
        const cur = (this.state.data && this.state.data.currency) || "₹";
        if (abs >= 1e7) {
            return `${cur} ${(v / 1e7).toFixed(2)} Cr`;
        }
        if (abs >= 1e5) {
            return `${cur} ${(v / 1e5).toFixed(2)} L`;
        }
        return `${cur} ${this.fmt(v, 0)}`;
    }

    renderChart() {
        const el = this.chartRef.el;
        const data = this.state.data;
        if (!el || !data || !window.Chart) {
            return;
        }
        if (this.chart) {
            this.chart.destroy();
        }
        const datasets = data.trend.series.map((s) => ({
            label: s.name,
            data: s.data,
            backgroundColor: s.color,
            borderRadius: 4,
            stack: "qty",
            maxBarThickness: 28,
        }));
        this.chart = new window.Chart(el, {
            type: "bar",
            data: { labels: data.trend.labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                plugins: {
                    legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `${ctx.dataset.label}: ${this.fmt(ctx.parsed.y)} Ltr`,
                            footer: (items) => {
                                const idx = items[0].dataIndex;
                                return `Sale value: ${this.money(data.trend.totals[idx])}`;
                            },
                        },
                    },
                },
                scales: {
                    x: { stacked: true, grid: { display: false } },
                    y: { stacked: true, beginAtZero: true, title: { display: true, text: "Litres" } },
                },
            },
        });
    }

    // ---------------------------------------------------------------- navigation
    openAction(xmlid, extra = {}) {
        this.action.doAction(xmlid, extra);
    }

    openModel(resModel, domain, name, views = [[false, "list"], [false, "form"]], context = {}) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: resModel, name, domain, views, context });
    }

    openRecord(resModel, resId) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: resModel, res_id: resId, views: [[false, "form"]] });
    }

    openTodaySheets() {
        this.openModel("pump.shift.sheet", [["date", "=", this.state.date]], "Shift Sheets");
    }

    openTodaySlips() {
        this.openModel("pump.credit.sale", [["date", "=", this.state.date], ["state", "in", ["confirmed", "billed"]]], "Credit Slips");
    }

    openOverLimit() {
        this.openModel("res.partner", [["is_pump_customer", "=", true], ["pump_over_limit", "=", true]], "Customers over limit");
    }

    openCustomer(id) {
        this.openRecord("res.partner", id);
    }

    openSlip(id) {
        this.openRecord("pump.credit.sale", id);
    }

    openTank(id) {
        this.openRecord("pump.tank", id);
    }

    newSheet() {
        this.action.doAction({
            type: "ir.actions.act_window", res_model: "pump.shift.sheet", views: [[false, "form"]],
            context: { default_date: this.state.date },
        });
    }

    async askAI(ev) {
        ev?.preventDefault?.();
        const q = (this.state.aiQuestion || "").trim();
        if (!q) {
            return;
        }
        this.state.aiBusy = true;
        try {
            const res = await this.orm.call("pump.ai", "ask", [q], { mode: "staff" });
            this.state.aiAnswer = res.answer;
        } finally {
            this.state.aiBusy = false;
        }
    }

    quickAsk(q) {
        this.state.aiQuestion = q;
        return this.askAI();
    }

    formatAnswer(text) {
        return (text || "").split("\n");
    }

    newSlip() {
        this.action.doAction({
            type: "ir.actions.act_window", res_model: "pump.credit.sale", views: [[false, "form"]],
            context: { default_date: this.state.date },
        });
    }
}

registry.category("actions").add("bpcl_pump_dashboard", BpclPumpDashboard);
