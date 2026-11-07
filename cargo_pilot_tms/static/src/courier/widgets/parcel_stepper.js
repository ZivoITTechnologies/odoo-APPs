import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

const ORDER = ["draft", "booked", "picked_up", "in_transit", "out_for_delivery", "delivered"];

/**
 * Parcel milestones: Booked > Picked up > On the way > Out for delivery > Delivered,
 * with failed delivery and returns shown on the step where they happened.
 */
export class CpParcelStepper extends Component {
    static template = "cargo_pilot_tms.ParcelStepper";
    static props = { ...standardFieldProps };

    get steps() {
        const d = this.props.record.data;
        const state = d[this.props.name];
        const fmt = (v) => (v ? formatDateTime(v, { format: "dd MMM, HH:mm" }) : "");
        let reached = ORDER.indexOf(state);
        if (state === "ndr") {
            reached = ORDER.indexOf("out_for_delivery");
        } else if (["rto", "returned", "lost"].includes(state)) {
            reached = ORDER.indexOf("in_transit");
        }
        const defs = [
            { key: "booked", label: _t("Booked"), icon: "fa-file-text-o", time: fmt(d.booked_at) },
            { key: "picked_up", label: _t("Picked up"), icon: "fa-cube", time: fmt(d.picked_up_at) },
            { key: "in_transit", label: _t("On the way"), icon: "fa-truck", time: d.last_location || "" },
            { key: "out_for_delivery", label: _t("Out for delivery"), icon: "fa-motorcycle", time: "" },
            { key: "delivered", label: _t("Delivered"), icon: "fa-check", time: fmt(d.delivered_at) },
        ];
        return defs.map((s) => {
            const idx = ORDER.indexOf(s.key);
            let status = "todo";
            if (state === "cancelled") {
                status = "cancel";
            } else if (idx < reached || (idx === reached && s.key === "delivered")) {
                status = "done";
            } else if (idx === reached) {
                status = "current";
            }
            let flag = "";
            if (idx === reached && state === "ndr") {
                status = "late";
                flag = _t("Delivery failed");
            } else if (idx === reached && state === "rto") {
                status = "late";
                flag = _t("Returning");
            } else if (idx === reached && state === "returned") {
                status = "late";
                flag = _t("Returned");
            } else if (idx === reached && state === "lost") {
                status = "late";
                flag = _t("Lost");
            } else if (idx === reached && d.is_delayed) {
                flag = _t("Running late");
            }
            return { ...s, status, flag, time: status === "todo" ? "" : s.time };
        });
    }

    get cancelled() {
        return this.props.record.data[this.props.name] === "cancelled";
    }
}

registry.category("fields").add("cp_parcel_stepper", {
    component: CpParcelStepper,
    displayName: "Parcel milestones",
    supportedTypes: ["selection"],
});
