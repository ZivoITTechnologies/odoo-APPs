import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

/**
 * Live bid countdown on the vendor portal (FR-03): ticks every second and asks the server
 * every 5 seconds for the real deadline and state, so every vendor sees the same clock.
 * When the bid locks the page reloads to show the result.
 */
export class CpBidCountdown extends Interaction {
    static selector = ".cp-countdown";

    setup() {
        this.seconds = parseInt(this.el.dataset.seconds || "0", 10);
        this.liveUrl = this.el.dataset.liveUrl;
        this.clock = this.el.querySelector(".cp-countdown-clock");
        this.state = "live";
    }

    start() {
        this.render();
        this.tick = setInterval(() => this.step(), 1000);
        this.poll = setInterval(() => this.refresh(), 5000);
        this.registerCleanup(() => {
            clearInterval(this.tick);
            clearInterval(this.poll);
        });
    }

    render() {
        const s = Math.max(this.seconds, 0);
        const mm = String(Math.floor(s / 60)).padStart(2, "0");
        const ss = String(s % 60).padStart(2, "0");
        if (this.clock) {
            this.clock.textContent = `${mm}:${ss}`;
        }
        this.el.classList.toggle("cp-countdown-urgent", s <= 60);
    }

    step() {
        if (this.seconds > 0) {
            this.seconds -= 1;
            this.render();
        } else {
            this.refresh();
        }
    }

    async refresh() {
        if (!this.liveUrl) {
            return;
        }
        try {
            const response = await fetch(this.liveUrl, { credentials: "same-origin" });
            if (!response.ok) {
                return;
            }
            const data = await response.json();
            this.seconds = data.seconds_left;
            this.render();
            if (data.state !== "live") {
                clearInterval(this.tick);
                clearInterval(this.poll);
                window.location.reload();
            }
        } catch {
            // network hiccup: keep counting locally
        }
    }
}

registry.category("public.interactions").add("cargo_pilot_tms.bid_countdown", CpBidCountdown);
