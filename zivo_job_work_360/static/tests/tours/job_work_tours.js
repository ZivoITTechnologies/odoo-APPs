/** @odoo-module **/
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("zivo_job_work_dashboard_tour", {
    steps: () => [
        {
            content: "Dashboard KPIs are displayed",
            trigger: ".o_jw_dashboard .o_jw_kpi_value",
        },
        {
            content: "Pipeline is displayed",
            trigger: ".o_jw_dashboard .o_jw_pipeline_row",
        },
        {
            content: "Open the open jobs",
            trigger: ".o_jw_dashboard .o_jw_kpi:first",
            run: "click",
        },
        {
            content: "Job list is displayed",
            trigger: ".o_list_view .o_data_row",
        },
        {
            content: "Open the job",
            trigger: ".o_list_view .o_data_row:first .o_data_cell[name=name]",
            run: "click",
        },
        {
            content: "Job form is displayed",
            trigger: ".o_form_view .o_statusbar_status",
        },
        {
            content: "Costing tab",
            trigger: ".o_notebook .nav-link:contains(Costing)",
            run: "click",
        },
        {
            content: "Costing table is visible",
            trigger: ".o_jw_cost_table",
        },
    ],
});

registry.category("web_tour.tours").add("zivo_job_work_create_tour", {
    steps: () => [
        {
            trigger: ".o_list_button_add",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name=partner_id] input",
            run: "edit ABC Heat",
        },
        {
            trigger: ".o-autocomplete--dropdown-item:contains(ABC Heat Treat)",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name=job_type_id] input",
            run: "edit Heat Treatment",
        },
        {
            trigger: ".o-autocomplete--dropdown-item:contains(Heat Treatment)",
            run: "click",
        },
        {
            trigger: ".o_group .o_field_widget[name=product_id] input",
            run: "edit Brass Bush",
        },
        {
            trigger: ".o-autocomplete--dropdown-item:contains(Brass Bush)",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name=product_qty] input",
            run: "edit 25",
        },
        {
            trigger: ".o_field_widget[name=client_ref] input",
            run: "edit TOUR-REF-1",
        },
        {
            trigger: "div[name=material_ids] .o_field_x2many_list_row_add a",
            run: "click",
        },
        {
            trigger: "div[name=material_ids] .o_data_row .o_field_widget[name=product_id] input",
            run: "edit Brass Rod",
        },
        {
            trigger: ".o-autocomplete--dropdown-item:contains(Brass Rod)",
            run: "click",
        },
        {
            trigger: "div[name=material_ids] .o_data_row .o_field_widget[name=planned_qty] input",
            run: "edit 25",
        },
        {
            trigger: "button[name=action_plan]",
            run: "click",
        },
        {
            content: "Job is planned",
            trigger: ".o_statusbar_status .o_arrow_button_current:contains(Planned)",
        },
    ],
});

registry.category("web_tour.tours").add("zivo_e2e_create_issue", {
    steps: () => [
        { trigger: ".o_list_button_add", run: "click" },
        { trigger: ".o_field_widget[name=partner_id] input", run: "edit E2E Forge" },
        { trigger: ".o-autocomplete--dropdown-item:contains(E2E Forge)", run: "click" },
        { trigger: ".o_field_widget[name=job_type_id] input", run: "edit E2E Coating" },
        { trigger: ".o-autocomplete--dropdown-item:contains(E2E Coating)", run: "click" },
        { trigger: ".o_group .o_field_widget[name=product_id] input", run: "edit E2E Output" },
        { trigger: ".o-autocomplete--dropdown-item:contains(E2E Output)", run: "click" },
        { trigger: ".o_field_widget[name=product_qty] input", run: "edit 50" },
        { trigger: ".o_field_widget[name=rate] input", run: "edit 4" },
        { trigger: ".o_field_widget[name=client_ref] input", run: "edit E2E-REF" },
        { trigger: "div[name=material_ids] .o_field_x2many_list_row_add a", run: "click" },
        { trigger: "div[name=material_ids] .o_data_row .o_field_widget[name=product_id] input", run: "edit E2E Raw" },
        { trigger: ".o-autocomplete--dropdown-item:contains(E2E Raw)", run: "click" },
        { trigger: "div[name=material_ids] .o_data_row .o_field_widget[name=planned_qty] input", run: "edit 50" },
        { trigger: "button[name=action_plan]", run: "click" },
        { content: "planned", trigger: ".o_statusbar_status .o_arrow_button_current:contains(Planned)" },
        { trigger: "button[name=action_issue_material]:contains(Issue Material)", run: "click" },
        { content: "dispatch transfer opened", trigger: ".o_form_view button[name=button_validate]", run: "click" },
        { content: "dispatch done", trigger: ".o_statusbar_status .o_arrow_button_current:contains(Done)" },
    ],
});

registry.category("web_tour.tours").add("zivo_e2e_receive_inspect", {
    steps: () => [
        { trigger: "button[name=action_vendor_confirm_receipt]" },
        { trigger: "button[name=action_vendor_start]", run: "click" },
        { content: "processing", trigger: ".o_form_view:not(:has(button[name=action_vendor_start])) button[name=action_receive]" },
        { trigger: "button[name=action_receive]", run: "click" },
        { trigger: ".modal .o_field_widget[name=qty_received] input", run: "edit 50" },
        { trigger: ".modal .o_field_widget[name=vendor_challan_ref] input", run: "edit DC-77" },
        { trigger: ".modal button[name=action_confirm]", run: "click" },
        { content: "QC pending", trigger: "button[name=action_inspect]" },
        { trigger: "button[name=action_inspect]", run: "click" },
        { trigger: ".o_form_view .o_field_widget[name=qty_rejected] input", run: "edit 1" },
        { trigger: ".o_form_view .o_field_widget[name=qty_accepted] input", run: "edit 49" },
        { trigger: "button[name=action_done]", run: "click" },
        { content: "inspection done", trigger: ".o_statusbar_status .o_arrow_button_current:contains(Done)" },
    ],
});

registry.category("web_tour.tours").add("zivo_e2e_reconcile_close", {
    steps: () => [
        { trigger: "button[name=action_reconcile]" },
        { trigger: ".o_notebook .nav-link:contains(Costing)", run: "click" },
        { trigger: ".o_jw_cost_table" },
        { trigger: "button[name=action_reconcile]", run: "click" },
        { content: "reconciled", trigger: "button[name=action_close]" },
        { trigger: "button[name=action_close]", run: "click" },
        { content: "closed", trigger: ".o_form_view:not(:has(button[name=action_close])) .o_statusbar_status:contains(Closed)" },
    ],
});

registry.category("web_tour.tours").add("zivo_e2e_portal", {
    steps: () => [
        { content: "job page", trigger: ".o_jw_portal h3:contains(JW/)" },
        { trigger: "button:contains(Accept Job)", run: "click", expectUnloadPage: true },
        { content: "accepted", trigger: ".alert-success" },
        { trigger: "button[data-bs-target='#jw_report']", run: "click" },
        { trigger: "#jw_qty_completed", run: "edit 30" },
        { trigger: "#jw_note", run: "edit First batch" },
        { trigger: "#jw_report button[type=submit]", run: "click", expectUnloadPage: true },
        { content: "report saved", trigger: ".alert-success" },
        { content: "progress listed", trigger: "td:contains(30)" },
    ],
});
