/* Fuel portal — AI assistant widget (vanilla JS, no dependencies). */
(function () {
    "use strict";
    function esc(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }
    function fmt(s) {
        return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br/>");
    }
    function init() {
        const root = document.getElementById("bpf-ai");
        if (!root || root.dataset.ready) {
            return;
        }
        root.dataset.ready = "1";
        const body = root.querySelector(".bpf-ai-body");
        const form = root.querySelector(".bpf-ai-form");
        const input = form.querySelector("input");
        const csrf = (window.odoo && window.odoo.csrf_token) || "";
        let greeted = false;

        function add(role, html, links) {
            const div = document.createElement("div");
            div.className = "bpf-msg " + role;
            div.innerHTML = html;
            if (links && links.length) {
                const box = document.createElement("div");
                box.className = "bpf-msg-links";
                links.forEach((l) => {
                    const a = document.createElement("a");
                    a.href = l.url;
                    a.textContent = l.label;
                    box.appendChild(a);
                });
                div.appendChild(box);
            }
            body.appendChild(div);
            body.scrollTop = body.scrollHeight;
            return div;
        }
        function open() {
            root.classList.add("open");
            if (!greeted) {
                greeted = true;
                add("bot", fmt(`Hello **${root.dataset.name || ""}**! I can tell you your balance, bills, rates, ` +
                    `vehicle consumption & mileage, send statements and prepare fuel indents. What would you like to know?`));
            }
            setTimeout(() => input.focus(), 50);
        }
        async function ask(q) {
            if (!q.trim()) {
                return;
            }
            add("me", esc(q));
            input.value = "";
            const wait = add("bot typing", "<span></span><span></span><span></span>");
            try {
                const data = new URLSearchParams({ question: q, csrf_token: csrf });
                const resp = await fetch("/my/fuel/ai/ask", {
                    method: "POST", body: data, credentials: "same-origin",
                    headers: { "Content-Type": "application/x-www-form-urlencoded" },
                });
                const res = await resp.json();
                wait.remove();
                add("bot", fmt(res.answer || "Sorry, I could not answer that."), res.links);
            } catch (e) {
                wait.remove();
                add("bot", "Network problem — please try again.");
            }
        }
        document.querySelectorAll(".bpf-ai-open").forEach((el) =>
            el.addEventListener("click", (ev) => { ev.preventDefault(); open(); }));
        root.querySelector(".bpf-ai-close").addEventListener("click", () => root.classList.remove("open"));
        root.querySelectorAll(".bpf-ai-chips button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
        form.addEventListener("submit", (ev) => { ev.preventDefault(); ask(input.value); });
        window.bpfAsk = (q) => { open(); return ask(q); };
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
