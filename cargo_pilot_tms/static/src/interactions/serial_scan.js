import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

/**
 * Serial numbers at delivery: type them, or scan barcodes with the phone camera where the
 * browser supports the BarcodeDetector API (Chrome on Android). Shows a live count.
 */
export class CpSerialScan extends Interaction {
    static selector = ".cp-epod-form";
    dynamicContent = {
        ".cp-scan-btn": { "t-on-click": this.onToggle },
        ".cp-scan-target": { "t-on-input": this.updateCount },
    };

    setup() {
        this.area = this.el.querySelector(".cp-scan-target");
        this.button = this.el.querySelector(".cp-scan-btn");
        this.video = this.el.querySelector(".cp-scan-video");
        this.counter = this.el.querySelector(".cp-scan-count");
        this.stream = null;
        this.timer = null;
        this.registerCleanup(() => this.stop());
    }

    start() {
        if (!this.area) {
            return;
        }
        if (this.button && "BarcodeDetector" in window && navigator.mediaDevices) {
            this.button.classList.remove("d-none");
        }
        this.updateCount();
    }

    values() {
        return (this.area.value || "").split(/[\s,;]+/).map((v) => v.trim()).filter(Boolean);
    }

    updateCount() {
        if (this.counter) {
            const n = new Set(this.values()).size;
            this.counter.textContent = n ? `${n} scanned` : "";
        }
    }

    add(value) {
        const current = this.values();
        if (!current.includes(value)) {
            this.area.value = [...current, value].join("\n");
            this.updateCount();
            if (navigator.vibrate) {
                navigator.vibrate(60);
            }
        }
    }

    async onToggle() {
        if (this.stream) {
            this.stop();
            return;
        }
        try {
            this.stream = await this.waitFor(
                navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false })
            );
        } catch {
            this.stream = null;
            return;
        }
        // eslint-disable-next-line no-undef
        const detector = new BarcodeDetector();
        this.video.srcObject = this.stream;
        this.video.classList.remove("d-none");
        await this.video.play();
        this.button.textContent = "Stop";
        this.timer = setInterval(async () => {
            try {
                const codes = await detector.detect(this.video);
                for (const code of codes) {
                    this.add(code.rawValue.trim());
                }
            } catch {
                // frame not ready yet
            }
        }, 400);
    }

    stop() {
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
        if (this.stream) {
            for (const track of this.stream.getTracks()) {
                track.stop();
            }
            this.stream = null;
        }
        if (this.video) {
            this.video.classList.add("d-none");
        }
        if (this.button) {
            this.button.innerHTML = '<i class="fa fa-barcode me-1"></i>Scan';
        }
    }
}

registry.category("public.interactions").add("cargo_pilot_tms.serial_scan", CpSerialScan);
