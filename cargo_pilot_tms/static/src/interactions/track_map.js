/* global L */
import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

/**
 * Tracking page map: planned route, travelled path and the truck's position.
 */
export class CpTrackMap extends Interaction {
    static selector = ".cp-leaflet";

    start() {
        if (typeof L === "undefined") {
            return;
        }
        const data = JSON.parse(this.el.dataset.map || "{}");
        const map = L.map(this.el, { scrollWheelZoom: false });
        this.registerCleanup(() => map.remove());
        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 18,
            attribution: "&copy; OpenStreetMap contributors",
        }).addTo(map);
        const bounds = [];
        const pin = (latlng, color, label) => {
            L.circleMarker(latlng, { radius: 7, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 })
                .bindTooltip(label)
                .addTo(map);
            bounds.push(latlng);
        };
        if (data.origin && data.destination) {
            L.polyline([data.origin, data.destination], { color: "#8a96a3", weight: 2, dashArray: "6 8" }).addTo(map);
        }
        if (data.path && data.path.length > 1) {
            L.polyline(data.path, { color: "#2e7d6b", weight: 4, opacity: 0.85 }).addTo(map);
        }
        if (data.origin) {
            pin(data.origin, "#5c6873", data.origin_name || "Origin");
        }
        if (data.destination) {
            pin(data.destination, "#1f2a33", data.destination_name || "Destination");
        }
        if (data.current) {
            const color = data.late ? "#b5525b" : "#2e7d6b";
            L.circleMarker(data.current, { radius: 11, color: "#fff", weight: 3, fillColor: color, fillOpacity: 1 })
                .bindTooltip(data.vehicle || "Truck", { permanent: true, direction: "top", offset: [0, -10] })
                .addTo(map);
            bounds.push(data.current);
        }
        if (bounds.length) {
            map.fitBounds(bounds, { padding: [40, 40], maxZoom: 11 });
        } else {
            map.setView([21.0, 78.9], 5);
        }
    }
}

registry.category("public.interactions").add("cargo_pilot_tms.track_map", CpTrackMap);
