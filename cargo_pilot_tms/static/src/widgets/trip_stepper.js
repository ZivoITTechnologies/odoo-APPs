import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

const ORDER = ["draft", "assigned", "reported", "loading", "in_transit", "arrived", "delivered", "closed"];

/**
 * Visual milestone tracker for a shipment: five plain-language steps with icons
 * and the time each one happened. Wraps into two rows on phones.
 */
export class CpTripStepper extends Component {
    static template = "cargo_pilot_tms.TripStepper";
    static props = { ...standardFieldProps };

    get steps() {
        const d = this.props.record.data;
        const state = d[this.props.name];
        const idx = ORDER.indexOf(state);
        const fmt = (v) => (v ? formatDateTime(v, { format: "dd MMM, HH:mm" }) : "");
        const defs = [
            { key: "planned", label: _t("Booked"), icon: "fa-calendar-check-o", at: 0, time: fmt(d.planned_dispatch) },
            { key: "vehicle", label: _t("Truck assigned"), icon: "fa-id-card-o", at: 1, time: d.vehicle_number || "" },
            { key: "loading", label: _t("Loading"), icon: "fa-cubes", at: 2, time: "" },
            { key: "transit", label: _t("On the road"), icon: "fa-truck", at: 4, time: fmt(d.actual_dispatch) },
            { key: "arrived", label: _t("Arrived"), icon: "fa-map-marker", at: 5, time: fmt(d.actual_arrival) },
            { key: "delivered", label: _t("Delivered"), icon: "fa-check", at: 6, time: fmt(d.actual_delivery) },
        ];
        return defs.map((s, i) => {
            const next = defs[i + 1];
            let status = "todo";
            if (state === "cancel") {
                status = "cancel";
            } else if (idx >= s.at && (!next || idx < next.at)) {
                status = idx >= ORDER.indexOf("delivered") && s.key === "delivered" ? "done" : "current";
            } else if (idx > s.at) {
                status = "done";
            }
            if (s.key === "transit" && status === "current" && d.is_late) {
                status = "late";
            }
            return { ...s, status };
        });
    }

    get cancelled() {
        return this.props.record.data[this.props.name] === "cancel";
    }
}

registry.category("fields").add("cp_trip_stepper", {
    component: CpTripStepper,
    displayName: "Shipment milestones",
    supportedTypes: ["selection"],
});
