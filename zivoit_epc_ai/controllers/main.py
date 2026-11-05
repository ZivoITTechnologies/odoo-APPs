"""REST API for ERP / data-platform integration.

Authentication: Odoo API key of an EPC user, sent as ``Authorization: Bearer <key>``.
All responses are JSON. Errors return {"error": {"code", "message"}} with a matching HTTP status.
"""
import json
import logging

from odoo import http
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)
PREFIX = "/api/zivoit_epc/v1"


def _response(payload, status=200):
    return request.make_json_response(payload, status=status)


def _error(code, message, status):
    return _response({"error": {"code": code, "message": message}}, status=status)


def _body():
    raw = request.httprequest.get_data(as_text=True) or "{}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValidationError("Request body is not valid JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object.")
    return data


def _call(method, *args):
    try:
        result = getattr(request.env["epc.api"], method)(*args)
        return _response(result)
    except AccessError as exc:
        request.env.cr.rollback()
        return _error("forbidden", str(exc), 403)
    except MissingError as exc:
        request.env.cr.rollback()
        return _error("not_found", str(exc), 404)
    except (ValidationError, UserError) as exc:
        request.env.cr.rollback()
        return _error("invalid", str(exc), 422)


class EpcApiController(http.Controller):

    @http.route(PREFIX + "/projects", type="http", auth="bearer", methods=["GET"], csrf=False, save_session=False)
    def projects(self, **params):
        return _call("list_projects", params)

    @http.route(PREFIX + "/projects/<int:project_id>", type="http", auth="bearer", methods=["GET"], csrf=False,
                save_session=False)
    def project(self, project_id, **_kw):
        return _call("get_project", project_id)

    @http.route(PREFIX + "/projects/<int:project_id>/activities", type="http", auth="bearer", methods=["GET"],
                csrf=False, save_session=False)
    def activities(self, project_id, **_kw):
        return _call("get_activities", project_id)

    @http.route(PREFIX + "/projects/<int:project_id>/risks", type="http", auth="bearer", methods=["GET"],
                csrf=False, save_session=False)
    def risks(self, project_id, **_kw):
        return _call("get_risks", project_id)

    @http.route(PREFIX + "/projects/<int:project_id>/materials", type="http", auth="bearer", methods=["POST"],
                csrf=False, save_session=False)
    def materials(self, project_id, **_kw):
        try:
            body = _body()
        except ValidationError as exc:
            return _error("invalid", str(exc), 400)
        return _call("push_materials", project_id, body)

    @http.route(PREFIX + "/projects/<int:project_id>/progress", type="http", auth="bearer", methods=["POST"],
                csrf=False, save_session=False)
    def progress(self, project_id, **_kw):
        try:
            body = _body()
        except ValidationError as exc:
            return _error("invalid", str(exc), 400)
        return _call("push_progress", project_id, body)

    @http.route(PREFIX + "/projects/<int:project_id>/predict", type="http", auth="bearer", methods=["POST"],
                csrf=False, save_session=False)
    def predict(self, project_id, **_kw):
        return _call("run_prediction", project_id)
