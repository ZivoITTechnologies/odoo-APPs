import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Layout } from "@web/search/layout";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { Component, onWillStart, onWillDestroy, useState } from "@odoo/owl";

const REFRESH_MS = 5000;

export class ZivoShopifyDashboard extends Component {
    static template = "zivoit_shopify_connector.Dashboard";
    static components = { Layout };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.display = { controlPanel: {} };
        this.state = useState({
            question: "",
            data: null,
            instanceId: false,
            answer: "",
            engine: "",
            asking: false,
        });
        onWillStart(() => this.load());
        this.timer = setInterval(() => {
            if (!document.hidden) {
                this.load();
            }
        }, REFRESH_MS);
        onWillDestroy(() => clearInterval(this.timer));
    }

    async load() {
        this.state.data = await this.orm.silent.call("zivo.shopify.dashboard", "get_data", [this.state.instanceId || false]);
    }

    async onStoreChange(ev) {
        this.state.instanceId = parseInt(ev.target.value) || false;
        await this.load();
    }

    get currentStore() {
        const list = this.state.data.instances;
        return list.find((store) => store.id === this.state.instanceId) || list[0];
    }

    get budget() {
        const store = this.currentStore;
        if (!store || !store.api_maximum) {
            return 100;
        }
        return Math.round((store.api_available / store.api_maximum) * 100);
    }

    get bars() {
        const series = this.state.data.series;
        const peak = Math.max(1, ...series.map((point) => point.revenue));
        return series.map((point, index) => {
            const height = Math.round((point.revenue / peak) * 150);
            return {
                ...point,
                x: 12 + index * 44,
                y: 170 - height,
                height: Math.max(height, 2),
                showLabel: index % 2 === 1,
            };
        });
    }

    formatMoney(value) {
        return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
    }

    open(xmlid) {
        return this.action.doAction(`zivoit_shopify_connector.${xmlid}`);
    }

    onQuestionKey(ev) {
        if (ev.key === "Enter") {
            this.ask();
        }
    }

    async ask() {
        const question = this.state.question.trim();
        if (!question || this.state.asking) {
            return;
        }
        this.state.asking = true;
        try {
            const result = await this.orm.call("zivo.shopify.dashboard", "ask_copilot", [question, this.state.instanceId || false]);
            this.state.answer = result.answer;
            this.state.engine = result.engine === "llm" ? _t("Answered by your LLM") : _t("Answered by the local engine");
        } finally {
            this.state.asking = false;
        }
    }
}

registry.category("actions").add("zivo_shopify_dashboard", ZivoShopifyDashboard);
