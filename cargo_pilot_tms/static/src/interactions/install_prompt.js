import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

/**
 * Offer "add to home screen" for the driver page where the browser supports it.
 */
export class CpInstallPrompt extends Interaction {
    static selector = ".cp-install";
    dynamicContent = {
        ".cp-install-btn": { "t-on-click": this.onInstall },
    };

    setup() {
        this.prompt = null;
    }

    start() {
        const standalone = window.matchMedia && window.matchMedia("(display-mode: standalone)").matches;
        if (standalone) {
            return;
        }
        this.addListener(window, "beforeinstallprompt", (ev) => {
            ev.preventDefault();
            this.prompt = ev;
            this.show();
        });
        if (/iphone|ipad|ipod/i.test(window.navigator.userAgent)) {
            this.el.querySelector(".cp-install-btn").textContent = "How?";
            this.show();
        }
    }

    show() {
        this.el.classList.remove("d-none");
        this.el.classList.add("d-flex");
    }

    hide() {
        this.el.classList.add("d-none");
        this.el.classList.remove("d-flex");
    }

    async onInstall() {
        if (this.prompt) {
            this.prompt.prompt();
            await this.waitFor(this.prompt.userChoice);
            this.prompt = null;
            this.hide();
        } else {
            window.alert('Tap the Share button, then "Add to Home Screen".');
        }
    }
}

registry.category("public.interactions").add("cargo_pilot_tms.install_prompt", CpInstallPrompt);
