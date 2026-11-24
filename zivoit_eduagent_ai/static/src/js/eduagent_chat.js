/* ZivoIT EduAgent AI - framework-free chat widget for website / portal pages. */
(function () {
    "use strict";

    function csrf() {
        return (window.odoo && window.odoo.csrf_token) || "";
    }

    function store(key, value) {
        try {
            if (value === undefined) { return window.localStorage.getItem(key); }
            if (value === null) { window.localStorage.removeItem(key); return null; }
            window.localStorage.setItem(key, value);
        } catch (e) { /* storage unavailable */ }
        return null;
    }

    function el(tag, cls, html) {
        var node = document.createElement(tag);
        if (cls) { node.className = cls; }
        if (html !== undefined) { node.innerHTML = html; }
        return node;
    }

    function post(url, payload) {
        return fetch(url + "?csrf_token=" + encodeURIComponent(csrf()), {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload),
        }).then(function (r) { return r.json(); });
    }

    function mount(root) {
        if (root.dataset.mounted) { return; }
        root.dataset.mounted = "1";
        var agent = root.dataset.agent || "admissions";
        var scope = root.dataset.channelId || root.dataset.slideId || "";
        var storeKey = "eduagent_token_" + agent + "_" + scope;
        var token = store(storeKey);
        var busy = false;

        var box = el("div", "o_eduagent_box");
        box.style.height = (root.dataset.height || 520) + "px";
        var head = el("div", "o_eduagent_head");
        head.appendChild(el("span", "o_eduagent_dot"));
        head.appendChild(el("span", "o_eduagent_title"));
        head.lastChild.textContent = root.dataset.title || "AI assistant";
        var reset = el("button", "o_eduagent_reset", "New chat");
        reset.type = "button";
        head.appendChild(reset);
        var list = el("div", "o_eduagent_msgs");
        var form = el("form", "o_eduagent_form");
        var input = el("textarea", "o_eduagent_input");
        input.rows = 1;
        input.placeholder = "Type your message...";
        var send = el("button", "o_eduagent_send", "Send");
        send.type = "submit";
        form.appendChild(input);
        form.appendChild(send);
        var foot = el("div", "o_eduagent_disclosure");
        box.appendChild(head);
        box.appendChild(list);
        box.appendChild(form);
        box.appendChild(foot);
        root.appendChild(box);

        function add(role, html, extra) {
            var row = el("div", "o_eduagent_msg o_eduagent_" + role);
            var bubble = el("div", "o_eduagent_bubble", html);
            row.appendChild(bubble);
            if (extra) { bubble.appendChild(extra); }
            list.appendChild(row);
            list.scrollTop = list.scrollHeight;
            return row;
        }

        function textToHtml(text) {
            var div = document.createElement("div");
            div.textContent = text;
            return div.innerHTML.replace(/\n/g, "<br/>");
        }

        function sourcesNode(sources) {
            if (!sources || !sources.length) { return null; }
            var wrap = el("div", "o_eduagent_sources");
            wrap.appendChild(document.createTextNode("Sources: "));
            sources.forEach(function (s, i) {
                if (i) { wrap.appendChild(document.createTextNode(" | ")); }
                var a = document.createElement(s.url ? "a" : "span");
                if (s.url) { a.href = s.url; a.target = "_blank"; a.rel = "noopener"; }
                a.textContent = s.title;
                wrap.appendChild(a);
            });
            return wrap;
        }

        function payload(message) {
            var p = {agent: agent, message: message, token: token};
            if (root.dataset.channelId) { p.channel_id = root.dataset.channelId; }
            if (root.dataset.slideId) { p.slide_id = root.dataset.slideId; }
            return p;
        }

        function handleError(res) {
            if (res.error === "login") {
                add("assistant", textToHtml(res.message || "Please sign in.") +
                    ' <a href="' + res.login_url + '">Sign in</a>');
                form.style.display = "none";
                return true;
            }
            if (res.error) {
                add("assistant", textToHtml(res.error));
                return true;
            }
            return false;
        }

        function start() {
            list.innerHTML = "";
            post("/eduagent/chat", payload("")).then(function (res) {
                if (handleError(res)) { return; }
                token = res.token;
                store(storeKey, token);
                foot.textContent = res.disclosure || root.dataset.disclosure || "";
                var history = res.history || [];
                add("assistant", res.reply_html);
                if (history.length) {
                    history.forEach(function (m) {
                        add(m.role === "user" ? "user" : (m.role === "human" ? "human" : "assistant"), m.html);
                    });
                }
            }).catch(function () {
                add("assistant", "The assistant is unavailable right now. Please try again later.");
            });
        }

        function submit(ev) {
            if (ev) { ev.preventDefault(); }
            var text = input.value.trim();
            if (!text || busy) { return; }
            busy = true;
            send.disabled = true;
            input.value = "";
            add("user", textToHtml(text));
            var typing = add("assistant", '<span class="o_eduagent_typing"><i></i><i></i><i></i></span>');
            post("/eduagent/chat", payload(text)).then(function (res) {
                typing.remove();
                if (handleError(res)) { return; }
                token = res.token;
                store(storeKey, token);
                add(res.human ? "human" : "assistant", res.reply_html, sourcesNode(res.sources));
            }).catch(function () {
                typing.remove();
                add("assistant", "Sorry, something went wrong. Please try again.");
            }).finally(function () {
                busy = false;
                send.disabled = false;
                input.focus();
            });
        }

        form.addEventListener("submit", submit);
        input.addEventListener("keydown", function (ev) {
            if (ev.key === "Enter" && !ev.shiftKey) { submit(ev); }
        });
        reset.addEventListener("click", function () {
            token = null;
            store(storeKey, null);
            start();
        });
        start();
    }

    /* Floating AI tutor on eLearning course and lesson pages. */
    function tutorBubble() {
        var path = window.location.pathname;
        var slideMatch = path.match(/\/slides\/slide\/[^\/?#]*?-?(\d+)(?:$|[\/?#])/);
        var courseMatch = !slideMatch && path.match(/\/slides\/(?!slide\/|all|channel|tag)[^\/?#]*?-(\d+)(?:$|[\/?#])/);
        if (!slideMatch && !courseMatch) { return; }
        if (document.querySelector(".o_eduagent_chat")) { return; }
        var btn = el("button", "o_eduagent_fab", '<span>AI Tutor</span>');
        btn.type = "button";
        var panel = el("div", "o_eduagent_panel");
        var holder = el("div", "o_eduagent_chat");
        holder.dataset.agent = "tutor";
        holder.dataset.title = "AI Tutor";
        holder.dataset.height = "460";
        if (slideMatch) { holder.dataset.slideId = slideMatch[1]; } else { holder.dataset.channelId = courseMatch[1]; }
        panel.appendChild(holder);
        document.body.appendChild(panel);
        document.body.appendChild(btn);
        btn.addEventListener("click", function () {
            panel.classList.toggle("o_open");
            if (panel.classList.contains("o_open")) { mount(holder); }
        });
    }

    /* Integrity check: record whether the answer was pasted. */
    function pasteWatch() {
        var area = document.querySelector(".o_eduagent_oral_answer");
        var flag = document.querySelector(".o_eduagent_pasted");
        if (area && flag) {
            area.addEventListener("paste", function () { flag.value = "1"; });
        }
    }

    function init() {
        document.querySelectorAll(".o_eduagent_chat").forEach(mount);
        tutorBubble();
        pasteWatch();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
