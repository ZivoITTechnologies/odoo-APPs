import json
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .epc_activity import DISCIPLINES

DOC_STATUS = [
    ("draft", "In Preparation"),
    ("ifr", "Issued for Review"),
    ("ifa", "Issued for Approval"),
    ("approved_comments", "Approved with Comments (Code B)"),
    ("rejected", "Rejected / Resubmit (Code C)"),
    ("afc", "Approved for Construction"),
]

DOC_TYPES = [
    ("drawing", "Drawing"),
    ("specification", "Specification"),
    ("datasheet", "Datasheet"),
    ("calculation", "Calculation"),
    ("procedure", "Procedure / Method Statement"),
    ("vendor", "Vendor Document"),
    ("report", "Report"),
    ("other", "Other"),
]


class EpcDocument(models.Model):
    _name = "epc.document"
    _description = "EPC Master Document Register"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "project_id, doc_number"
    _rec_names_search = ["doc_number", "title"]

    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    doc_number = fields.Char(string="Document No.", required=True, tracking=True)
    title = fields.Char(required=True, tracking=True)
    discipline = fields.Selection(DISCIPLINES, default="engineering", required=True)
    doc_type = fields.Selection(DOC_TYPES, string="Type", default="drawing", required=True)
    originator = fields.Selection([("internal", "Internal"), ("vendor", "Vendor"), ("client", "Client")],
                                  default="internal", required=True)
    vendor_id = fields.Many2one("res.partner", string="Vendor")
    revision = fields.Char(default="A", required=True, tracking=True)
    status = fields.Selection(DOC_STATUS, default="draft", required=True, tracking=True)
    planned_afc_date = fields.Date(string="Planned AFC Date", tracking=True)
    actual_afc_date = fields.Date(string="Actual AFC Date", readonly=True, copy=False)
    is_late = fields.Boolean(compute="_compute_is_late", search="_search_is_late")
    days_late = fields.Integer(compute="_compute_is_late")
    responsible_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    revision_ids = fields.One2many("epc.document.revision", "document_id", string="Revision History", copy=False)
    needed_by_activity_ids = fields.Many2many("epc.activity", "epc_activity_document_rel", "document_id", "activity_id",
                                    string="Needed by Activities")
    transmittal_ids = fields.Many2many("epc.transmittal", "epc_transmittal_document_rel",
                                       "document_id", "transmittal_id", string="Transmittals", copy=False)

    _doc_number_uniq = models.Constraint(
        "unique(project_id, doc_number)",
        "Document numbers must be unique within a project.",
    )

    @api.depends("doc_number", "title", "revision")
    def _compute_display_name(self):
        for doc in self:
            doc.display_name = "%s rev %s — %s" % (doc.doc_number, doc.revision, doc.title)

    @api.depends("status", "planned_afc_date")
    def _compute_is_late(self):
        today = fields.Date.context_today(self)
        for doc in self:
            late = bool(doc.status != "afc" and doc.planned_afc_date and doc.planned_afc_date < today)
            doc.is_late = late
            doc.days_late = (today - doc.planned_afc_date).days if late else 0

    def _search_is_late(self, operator, value):
        if operator not in ("=", "!=", "in", "not in"):
            raise UserError(_("Unsupported search on late documents."))
        today = fields.Date.context_today(self)
        domain = [("status", "!=", "afc"), ("planned_afc_date", "<", today)]
        positive = (operator in ("=", "in") and bool(value)) or (operator in ("!=", "not in") and not value)
        if positive:
            return domain
        return ["|", ("status", "=", "afc"), "|", ("planned_afc_date", "=", False), ("planned_afc_date", ">=", today)]

    def _record_revision(self, note=""):
        for doc in self:
            self.env["epc.document.revision"].create({
                "document_id": doc.id,
                "revision": doc.revision,
                "status": doc.status,
                "note": note,
            })

    def write(self, vals):
        if vals.get("status") == "afc":
            vals.setdefault("actual_afc_date", fields.Date.context_today(self))
        result = super().write(vals)
        if "status" in vals or "revision" in vals:
            self._record_revision(note=self.env.context.get("epc_revision_note", ""))
        return result

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("status") == "afc":
                vals.setdefault("actual_afc_date", fields.Date.context_today(self))
        documents = super().create(vals_list)
        documents._record_revision(note=_("Created"))
        return documents

    @api.model
    def _next_revision(self, revision):
        revision = (revision or "").strip()
        if re.fullmatch(r"\d+", revision):
            return str(int(revision) + 1)
        if re.fullmatch(r"[A-Y]", revision.upper()):
            return chr(ord(revision.upper()) + 1)
        match = re.fullmatch(r"(.*?)(\d+)", revision)
        if match:
            return "%s%s" % (match.group(1), int(match.group(2)) + 1)
        return revision + "1" if revision else "A"

    def action_issue_for_review(self):
        self.write({"status": "ifr"})
        return True

    def action_issue_for_approval(self):
        self.write({"status": "ifa"})
        return True

    def action_approve_afc(self):
        self.write({"status": "afc"})
        return True

    def action_approved_comments(self):
        self.write({"status": "approved_comments"})
        return True

    def action_reject(self):
        self.write({"status": "rejected"})
        return True

    def action_new_revision(self):
        for doc in self:
            doc.with_context(epc_revision_note=_("New revision")).write({
                "revision": self._next_revision(doc.revision),
                "status": "draft",
                "actual_afc_date": False,
            })
        return True


class EpcDocumentRevision(models.Model):
    _name = "epc.document.revision"
    _description = "EPC Document Revision History"
    _order = "id desc"

    document_id = fields.Many2one("epc.document", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="document_id.company_id", store=True)
    revision = fields.Char(required=True)
    status = fields.Selection(DOC_STATUS, required=True)
    note = fields.Char()
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    date = fields.Datetime(default=fields.Datetime.now)


class EpcTransmittal(models.Model):
    _name = "epc.transmittal"
    _description = "EPC Document Transmittal"
    _inherit = ["mail.thread"]
    _order = "date desc, id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    partner_id = fields.Many2one("res.partner", string="Recipient", required=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    purpose = fields.Selection(
        [("review", "For Review"), ("approval", "For Approval"), ("construction", "For Construction"),
         ("information", "For Information")],
        default="approval", required=True)
    document_ids = fields.Many2many("epc.document", "epc_transmittal_document_rel", "transmittal_id",
                                    "document_id", string="Documents",
                                    domain="[('project_id', '=', project_id)]")
    cover_note = fields.Html(sanitize=True)
    state = fields.Selection([("draft", "Draft"), ("sent", "Sent")], default="draft", required=True, tracking=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("epc.transmittal") or _("New")
        return super().create(vals_list)

    def _fallback_cover_note(self):
        self.ensure_one()
        purpose = dict(self._fields["purpose"].selection)[self.purpose].lower()
        rows = "".join(
            "<li>%s rev %s — %s</li>" % (d.doc_number, d.revision, d.title) for d in self.document_ids)
        return (
            "<p>Dear %s,</p><p>Please find enclosed the following documents for project %s, "
            "transmitted %s:</p><ul>%s</ul><p>Kindly return your comments or approval code within "
            "14 days of receipt.</p><p>Regards,<br/>%s</p>"
            % (self.partner_id.name, self.project_id.name, purpose, rows, self.env.user.name))

    def action_ai_cover_note(self):
        for transmittal in self:
            if not transmittal.document_ids:
                raise UserError(_("Add documents to the transmittal first."))
            facts = {
                "recipient": transmittal.partner_id.name,
                "project": transmittal.project_id.name,
                "purpose": transmittal.purpose,
                "sender": self.env.user.name,
                "documents": [{"number": d.doc_number, "revision": d.revision, "title": d.title,
                               "status": d.status} for d in transmittal.document_ids],
            }
            answer = self.env["epc.ai.service"].ask(
                "transmittal_note",
                "Write a short, formal document transmittal cover note for an EPC project in simple HTML "
                "(p, ul, li, br only). Use only the facts given.",
                json.dumps(facts), transmittal._fallback_cover_note, record=transmittal)
            transmittal.cover_note = answer["result"].replace("```html", "").replace("```", "")
        return True

    def action_send(self):
        for transmittal in self:
            if not transmittal.document_ids:
                raise UserError(_("Add documents to the transmittal first."))
            if not transmittal.cover_note:
                transmittal.cover_note = transmittal._fallback_cover_note()
            if transmittal.purpose == "review":
                transmittal.document_ids.filtered(lambda d: d.status == "draft").action_issue_for_review()
            elif transmittal.purpose == "approval":
                transmittal.document_ids.filtered(
                    lambda d: d.status in ("draft", "ifr", "rejected")).action_issue_for_approval()
            transmittal.message_post(
                body=transmittal.cover_note,
                subject=_("Transmittal %s", transmittal.name),
                partner_ids=transmittal.partner_id.ids,
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
            for doc in transmittal.document_ids:
                doc.message_post(body=_("Sent on transmittal %s (%s).", transmittal.name, transmittal.purpose))
            transmittal.state = "sent"
        return True
