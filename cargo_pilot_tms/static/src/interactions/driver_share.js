import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";

const MIN_INTERVAL_MS = 30000;
const ACTIVE_STATES = ["assigned", "reported", "loading", "in_transit"];

/**
 * Driver page: share live location while the page is open.
 */
export class CpDriverShare extends Interaction {
    static selector = ".cp-driver";
    dynamicContent = {
        ".cp-share-toggle": { "t-on-click": this.onToggle },
    };

    setup() {
        this.token = this.el.dataset.token;
        this.watchId = null;
        this.lastSent = 0;
        this.registerCleanup(() => this.stopSharing(false));
    }

    start() {
        let resume = false;
        try {
            resume = sessionStorage.getItem("cp_share_" + this.token) === "1";
        } catch {
            // Storage blocked: the driver taps Start again.
        }
        if (resume && ACTIVE_STATES.includes(this.el.dataset.state)) {
            this.startSharing();
        }
    }

    setStatus(text) {
        const node = this.el.querySelector(".cp-share-status");
        if (node) {
            node.textContent = text;
        }
    }

    setButton(on) {
        const btn = this.el.querySelector(".cp-share-toggle");
        if (btn) {
            btn.textContent = on ? "Stop" : "Start";
            btn.classList.toggle("btn-primary", on);
            btn.classList.toggle("btn-outline-primary", !on);
        }
    }

    onToggle() {
        if (this.watchId === null) {
            this.startSharing();
        } else {
            this.stopSharing(true);
        }
    }

    startSharing() {
        if (!navigator.geolocation) {
            this.setStatus("This phone cannot share location.");
            return;
        }
        this.watchId = navigator.geolocation.watchPosition(
            (pos) => this.send(pos),
            () => this.setStatus("Allow location access to share your position."),
            { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 }
        );
        try {
            sessionStorage.setItem("cp_share_" + this.token, "1");
        } catch {
            // ignore
        }
        this.setButton(true);
        this.setStatus("Sharing location…");
    }

    stopSharing(forget) {
        if (this.watchId !== null) {
            navigator.geolocation.clearWatch(this.watchId);
            this.watchId = null;
        }
        if (forget) {
            try {
                sessionStorage.removeItem("cp_share_" + this.token);
            } catch {
                // ignore
            }
            this.setButton(false);
            this.setStatus("Location sharing is off.");
        }
    }

    async send(pos) {
        const now = Date.now();
        if (now - this.lastSent < MIN_INTERVAL_MS) {
            return;
        }
        this.lastSent = now;
        try {
            const res = await this.waitFor(
                rpc(`/cp/driver/${this.token}/ping`, {
                    latitude: pos.coords.latitude,
                    longitude: pos.coords.longitude,
                    speed: pos.coords.speed,
                })
            );
            if (res && res.ok) {
                const left = res.remaining_km ? ` · ${res.remaining_km} km to go` : "";
                this.setStatus(`Shared at ${new Date().toLocaleTimeString()}${left}`);
                if (res.state !== this.el.dataset.state) {
                    window.location.reload();
                }
            } else if (res && res.state) {
                this.stopSharing(true);
            }
        } catch {
            this.setStatus("No network. Will retry.");
            this.lastSent = 0;
        }
    }
}

registry.category("public.interactions").add("cargo_pilot_tms.driver_share", CpDriverShare);
