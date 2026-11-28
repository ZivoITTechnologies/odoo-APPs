/** @odoo-module **/
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("zivo_expense_claim_tour", {
    steps: () => [
        { trigger: ".modal .o_field_widget[name=date_from] input", run: "click" },
        { trigger: ".modal button[name=action_create]", run: "click" },
        { trigger: ".o_form_view .o_field_widget[name=name]:contains('FEC/')" },
        { trigger: ".o_field_one2many[name=line_ids] .o_data_row:contains('Mileage')" },
        { trigger: ".o_statusbar_buttons button[name=action_submit]", run: "click" },
        { trigger: ".o_statusbar_status button.o_arrow_button_current:contains('Submitted')" },
    ],
});
