import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

/**
 * ePOD form: finger/mouse signature pad and automatic GPS capture.
 */
export class CpEpodForm extends Interaction {
    static selector = ".cp-epod-form";
    dynamicContent = {
        ".cp-signature-clear": { "t-on-click": this.onClear },
        ".cp-signature-pad": {
            "t-on-pointerdown": this.onPointerDown,
            "t-on-pointermove": this.onPointerMove,
            "t-on-pointerup": this.onPointerUp,
            "t-on-pointercancel": this.onPointerUp,
        },
        _root: { "t-on-submit": this.onSubmit },
    };

    setup() {
        this.canvas = this.el.querySelector(".cp-signature-pad");
        this.drawn = false;
        this.drawing = false;
        if (this.canvas) {
            this.ctx = this.canvas.getContext("2d");
            this.ctx.lineWidth = 2.4;
            this.ctx.lineCap = "round";
            this.ctx.strokeStyle = "#1f2a33";
        }
    }

    start() {
        this.locate();
    }

    point(ev) {
        const rect = this.canvas.getBoundingClientRect();
        return {
            x: ((ev.clientX - rect.left) * this.canvas.width) / rect.width,
            y: ((ev.clientY - rect.top) * this.canvas.height) / rect.height,
        };
    }

    onPointerDown(ev) {
        this.drawing = true;
        this.canvas.setPointerCapture(ev.pointerId);
        const p = this.point(ev);
        this.ctx.beginPath();
        this.ctx.moveTo(p.x, p.y);
        ev.preventDefault();
    }

    onPointerMove(ev) {
        if (!this.drawing) {
            return;
        }
        const p = this.point(ev);
        this.ctx.lineTo(p.x, p.y);
        this.ctx.stroke();
        this.drawn = true;
        ev.preventDefault();
    }

    onPointerUp() {
        this.drawing = false;
    }

    onClear() {
        if (this.ctx) {
            this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
            this.drawn = false;
        }
    }

    setStatus(text) {
        const status = this.el.querySelector(".cp-geo-status");
        if (status) {
            status.textContent = text;
        }
    }

    locate() {
        if (!navigator.geolocation) {
            this.setStatus("Location not available");
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                if (this.isDestroyed) {
                    return;
                }
                this.el.querySelector(".cp-geo-lat").value = pos.coords.latitude.toFixed(7);
                this.el.querySelector(".cp-geo-lon").value = pos.coords.longitude.toFixed(7);
                this.setStatus(`Location captured (±${Math.round(pos.coords.accuracy)} m)`);
            },
            () => this.setStatus("Location permission denied"),
            { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
        );
    }

    onSubmit() {
        if (this.drawn && this.canvas) {
            this.el.querySelector(".cp-signature-data").value = this.canvas.toDataURL("image/png");
        }
    }
}

registry.category("public.interactions").add("cargo_pilot_tms.epod_form", CpEpodForm);
