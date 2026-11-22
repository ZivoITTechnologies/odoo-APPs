/** @odoo-module **/
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("zivoit_tender_dashboard_tour", {
    steps: () => [
        {
            content: "Dashboard KPIs are shown",
            trigger: ".o_zivoit_dashboard .o_zv_kpi_value",
        },
        {
            content: "Type a natural language query",
            trigger: ".o_zv_search input",
            run: "edit open tenders in railways in construction in SCR",
        },
        {
            content: "Run the agent",
            trigger: ".o_zv_search button",
            run: "click",
        },
        {
            content: "Agent understood the railway zone",
            trigger: ".o_zv_card .badge:contains('Zone: SCR')",
        },
        {
            content: "The matching tender is listed",
            trigger: ".o_zv_table td:contains('ROB tour test')",
            run: "click",
        },
        {
            content: "Tender form opens",
            trigger: ".o_form_view .o_field_widget[name='name']:contains('ROB tour test'), .o_form_view .o_field_widget[name='name'] textarea, .o_form_view .o_field_widget[name='name'] input",
        },
    ],
});
