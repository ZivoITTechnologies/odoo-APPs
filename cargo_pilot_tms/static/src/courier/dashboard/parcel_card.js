import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { CpControlTower } from "@cargo_pilot_tms/dashboard/control_tower";

const ACTIVE = ["booked", "picked_up", "in_transit", "out_for_delivery", "ndr", "rto"];

patch(CpControlTower.prototype, {
    onTile(key) {
        const parcels = {
            parcels_active: [[["state", "in", ACTIVE]], _t("Parcels on the way")],
            parcels_ofd: [[["state", "=", "out_for_delivery"]], _t("Out for delivery")],
            parcels_ndr: [[["state", "=", "ndr"]], _t("Delivery failed")],
            parcels_late: [[["is_delayed", "=", true]], _t("Late parcels")],
            parcels_cod: [[["cod_state", "=", "collected"]], _t("COD to receive")],
            parcels_draft: [[["state", "=", "draft"]], _t("Parcels to book")],
        };
        if (parcels[key]) {
            const [domain, name] = parcels[key];
            return this.open("cp.parcel", domain, name, ["list", "kanban", "form"]);
        }
        return super.onTile(key);
    },

    sendParcel() {
        this.action.doAction("cargo_pilot_tms.action_cp_parcel_new");
    },
});
