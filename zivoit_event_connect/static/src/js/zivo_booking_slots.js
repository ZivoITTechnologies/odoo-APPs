/** ZivoIT Event Connect - equipment booking slot picker (keeps selection contiguous) */
import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

export class ZivoBookingSlots extends Interaction {
    static selector = ".o_zivo_booking_form";
    dynamicContent = {
        ".o_zivo_slot_input": { "t-on-change": this.onSlotChange },
        ".o_zivo_date_input": { "t-on-change": this.onDateChange },
    };

    onDateChange(ev) {
        const url = new URL(window.location.href);
        url.searchParams.set("date", ev.currentTarget.value);
        url.searchParams.delete("error");
        window.location.href = url.toString();
    }

    onSlotChange(ev) {
        const inputs = [...this.el.querySelectorAll(".o_zivo_slot_input:not(:disabled)")];
        const all = [...this.el.querySelectorAll(".o_zivo_slot_input")];
        const checked = all.map((el, idx) => (el.checked ? idx : -1)).filter((i) => i >= 0);
        if (checked.length > 1) {
            const first = Math.min(...checked);
            const last = Math.max(...checked);
            // if a gap contains an unavailable slot, restart selection from the clicked slot
            const gapBlocked = all.slice(first, last + 1).some((el) => el.disabled);
            if (gapBlocked) {
                all.forEach((el) => (el.checked = el === ev.currentTarget));
            } else {
                all.slice(first, last + 1).forEach((el) => (el.checked = true));
            }
        }
        const count = inputs.filter((el) => el.checked).length;
        const counter = this.el.querySelector(".o_zivo_slot_count");
        if (counter) {
            counter.textContent = count;
        }
        const submit = this.el.querySelector(".o_zivo_book_submit");
        if (submit) {
            submit.disabled = count === 0;
        }
    }
}

registry.category("public.interactions").add("zivoit_event_connect.booking_slots", ZivoBookingSlots);
