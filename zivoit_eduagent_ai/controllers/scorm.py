# -*- coding: utf-8 -*-
"""SCORM 1.2 / 2004 player: serves package files from the stored zip and exposes the SCORM
runtime API to the content frame, saving progress and completing the eLearning lesson."""
import json
import mimetypes

from odoo import http
from odoo.http import request
from odoo.tools import html_escape

PLAYER = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>%(title)s</title>
<style>html,body{margin:0;height:100%%;font-family:system-ui,sans-serif}
#bar{height:44px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;
background:#14213d;color:#fff;font-size:14px}#bar a{color:#fff}#sco{border:0;width:100%%;height:calc(100%% - 44px)}</style>
</head><body>
<div id="bar"><span>%(title)s</span><span id="st">Not started</span><a href="%(back)s">Back to course</a></div>
<iframe id="sco" src="%(src)s" allow="fullscreen"></iframe>
<script>
(function(){
  var cmi = %(cmi)s, dirty = false, commitUrl = "%(commit)s", csrf = "%(csrf)s";
  function setStatus(){ var s = cmi["cmi.core.lesson_status"] || cmi["cmi.completion_status"] || "incomplete";
    document.getElementById("st").textContent = "Status: " + s; }
  function commit(){ if(!dirty) return "true"; dirty = false;
    var body = JSON.stringify({cmi: cmi});
    if (navigator.sendBeacon && document.visibilityState === "hidden") {
      navigator.sendBeacon(commitUrl + "?csrf_token=" + encodeURIComponent(csrf), new Blob([body], {type: "application/json"}));
    } else {
      fetch(commitUrl + "?csrf_token=" + encodeURIComponent(csrf), {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"}, body: body});
    }
    setStatus(); return "true"; }
  var lastError = "0";
  function getV(k){ lastError = "0"; return (cmi[k] === undefined || cmi[k] === null) ? "" : String(cmi[k]); }
  function setV(k, v){ lastError = "0"; cmi[k] = String(v); dirty = true; return "true"; }
  window.API = {LMSInitialize: function(){ if(!cmi["cmi.core.lesson_status"]) cmi["cmi.core.lesson_status"]="incomplete"; setStatus(); return "true"; },
    LMSFinish: function(){ dirty = true; return commit(); }, LMSGetValue: getV, LMSSetValue: setV,
    LMSCommit: function(){ return commit(); }, LMSGetLastError: function(){ return lastError; },
    LMSGetErrorString: function(){ return ""; }, LMSGetDiagnostic: function(){ return ""; }};
  window.API_1484_11 = {Initialize: function(){ return "true"; }, Terminate: function(){ dirty = true; return commit(); },
    GetValue: getV, SetValue: setV, Commit: function(){ return commit(); },
    GetLastError: function(){ return lastError; }, GetErrorString: function(){ return ""; }, GetDiagnostic: function(){ return ""; }};
  window.addEventListener("visibilitychange", function(){ if (document.visibilityState === "hidden") commit(); });
  setInterval(commit, 30000);
})();
</script></body></html>"""

COMPLETE_STATES = {"completed", "passed"}


class EduagentScormController(http.Controller):

    def _check_item(self, item_id):
        item = request.env["eduagent.scorm.item"].sudo().browse(item_id).exists()
        if not item:
            return None
        user = request.env.user
        if user._is_internal():
            return item
        channel = item.package_id.channel_id
        member = request.env["slide.channel.partner"].sudo().search_count(
            [("channel_id", "=", channel.id), ("partner_id", "=", user.partner_id.id)])
        return item if member else None

    @http.route("/eduagent/scorm/play/<int:item_id>", type="http", auth="user", website=True, sitemap=False)
    def play(self, item_id, **kw):
        item = self._check_item(item_id)
        if not item:
            raise request.not_found()
        track = request.env["eduagent.scorm.tracking"].sudo().search(
            [("item_id", "=", item.id), ("partner_id", "=", request.env.user.partner_id.id)], limit=1)
        cmi = json.loads(track.cmi_json) if track and track.cmi_json else {}
        cmi.setdefault("cmi.core.student_id", str(request.env.user.partner_id.id))
        cmi.setdefault("cmi.core.student_name", request.env.user.partner_id.name or "")
        cmi.setdefault("cmi.learner_id", str(request.env.user.partner_id.id))
        cmi.setdefault("cmi.learner_name", request.env.user.partner_id.name or "")
        back = item.slide_id.website_url or item.package_id.channel_id.website_url or "/slides"
        html = PLAYER % {
            "title": html_escape(item.name),
            "back": html_escape(back),
            "src": "/eduagent/scorm/file/%s/%s" % (item.package_id.id, item.launch_path),
            "cmi": json.dumps(cmi).replace("</", "<\\/"),
            "commit": "/eduagent/scorm/commit/%s" % item.id,
            "csrf": request.csrf_token(),
        }
        return request.make_response(html, headers=[("Content-Type", "text/html; charset=utf-8")])

    @http.route("/eduagent/scorm/file/<int:package_id>/<path:path>", type="http", auth="user", sitemap=False)
    def file(self, package_id, path, **kw):
        package = request.env["eduagent.scorm.package"].sudo().browse(package_id).exists()
        if not package or not package.item_ids or not self._check_item(package.item_ids[0].id):
            raise request.not_found()
        data = package._read_file(path)
        if data is None:
            raise request.not_found()
        mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return request.make_response(data, headers=[
            ("Content-Type", mimetype), ("Cache-Control", "private, max-age=3600"),
            ("X-Content-Type-Options", "nosniff")])

    @http.route("/eduagent/scorm/commit/<int:item_id>", type="http", auth="user", methods=["POST"], sitemap=False)
    def commit(self, item_id, **kw):
        item = self._check_item(item_id)
        if not item:
            return request.make_json_response({"ok": False}, status=404)
        data = request.get_json_data() or {}
        cmi = data.get("cmi") or {}
        if not isinstance(cmi, dict):
            return request.make_json_response({"ok": False}, status=400)
        partner = request.env.user.partner_id
        Track = request.env["eduagent.scorm.tracking"].sudo()
        track = Track.search([("item_id", "=", item.id), ("partner_id", "=", partner.id)], limit=1)
        status = cmi.get("cmi.core.lesson_status") or cmi.get("cmi.completion_status") or ""
        success = cmi.get("cmi.success_status") or ""
        try:
            score = float(cmi.get("cmi.core.score.raw") or cmi.get("cmi.score.raw") or 0)
        except (TypeError, ValueError):
            score = 0.0
        vals = {"item_id": item.id, "partner_id": partner.id, "lesson_status": status or success,
                "score_raw": score, "suspend_data": (cmi.get("cmi.suspend_data") or "")[:64000],
                "lesson_location": cmi.get("cmi.core.lesson_location") or cmi.get("cmi.location") or "",
                "total_time": cmi.get("cmi.core.session_time") or cmi.get("cmi.session_time") or "",
                "cmi_json": json.dumps(cmi)[:200000]}
        if track:
            track.write(vals)
        else:
            Track.create(vals)
        if (status in COMPLETE_STATES or success == "passed") and item.slide_id:
            is_member = request.env["slide.channel.partner"].sudo().search_count([
                ("channel_id", "=", item.slide_id.channel_id.id), ("partner_id", "=", partner.id)])
            slide = item.slide_id.with_user(request.env.user)
            if is_member and not slide.user_has_completed:
                slide._action_mark_completed()
        return request.make_json_response({"ok": True})
