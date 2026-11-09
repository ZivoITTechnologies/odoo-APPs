/** @odoo-module **/
// Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { browser } from "@web/core/browser/browser";

export class ZivoMcpDashboard extends Component {
    static template = "zivoit_mcp_connector.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ data: null });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.data = await this.orm.call("zivo.mcp.dashboard", "get_dashboard_data", []);
    }

    barHeight(value, max) {
        return `${Math.max(value ? 4 : 0, Math.round((100 * value) / (max || 1)))}%`;
    }

    toolWidth(value) {
        return `${Math.round((100 * value) / (this.state.data.max_tool || 1))}%`;
    }

    openConnector(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivo.mcp.connector",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    newConnector() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivo.mcp.connector",
            views: [[false, "form"]],
        });
    }

    openLogs(domain = []) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Audit Log",
            res_model: "zivo.mcp.log",
            domain,
            views: [[false, "list"], [false, "form"]],
        });
    }

    openLog(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "zivo.mcp.log",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    async copyEndpoint() {
        await browser.navigator.clipboard.writeText(this.state.data.endpoint);
        this.notification.add("MCP endpoint copied", { type: "success" });
    }
}

registry.category("actions").add("zivo_mcp_dashboard", ZivoMcpDashboard);
