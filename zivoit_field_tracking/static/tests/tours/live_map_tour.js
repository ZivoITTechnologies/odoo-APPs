import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("zivo_track_live_map_tour", {
    steps: () => [
        {
            content: "Live map is rendered with Leaflet",
            trigger: ".zv-live .zv-map.leaflet-container",
        },
        {
            content: "A tracked salesperson is listed",
            trigger: ".zv-user-row:contains('Mitchell Admin')",
            run: "click",
        },
        {
            content: "Marker is on the map",
            trigger: ".zv-marker-pin",
        },
        {
            content: "Open today's route",
            trigger: ".zv-user-row:contains('Mitchell Admin') .zv-route-btn",
            run: "click",
        },
        {
            content: "Route replay legend is shown",
            trigger: ".zv-legend",
        },
        {
            content: "Back to live map",
            trigger: ".zv-live-actions button:contains('Live Map')",
            run: "click",
        },
        {
            content: "Status chips are back",
            trigger: ".zv-chip:contains('Live')",
        },
    ],
});
