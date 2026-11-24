# -*- coding: utf-8 -*-
import base64

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape


class EduagentScormImportWizard(models.TransientModel):
    _name = "eduagent.scorm.import.wizard"
    _description = "Import SCORM Package"

    package_file = fields.Binary(string="SCORM .zip", required=True)
    filename = fields.Char()
    channel_id = fields.Many2one("slide.channel", string="Add to Course",
                                 help="Leave empty to create a new draft course.")

    def action_import(self):
        self.ensure_one()
        raw = base64.b64decode(self.package_file or b"")
        if not raw:
            raise UserError(_("Upload a SCORM zip file."))
        Package = self.env["eduagent.scorm.package"]
        info = Package.parse_manifest(raw)
        channel = self.channel_id or self.env["slide.channel"].create({
            "name": info["title"], "channel_type": "training", "is_published": False})
        attachment = self.env["ir.attachment"].create({
            "name": self.filename or "scorm.zip", "raw": raw, "res_model": "eduagent.scorm.package",
            "mimetype": "application/zip"})
        package = Package.create({"name": info["title"], "channel_id": channel.id,
                                  "attachment_id": attachment.id, "version": info["version"]})
        attachment.res_id = package.id
        seq = max(channel.slide_ids.mapped("sequence") or [0]) + 1
        for i, item in enumerate(info["items"]):
            sco = self.env["eduagent.scorm.item"].create({
                "package_id": package.id, "sequence": i, "name": item["name"], "launch_path": item["launch_path"]})
            slide = self.env["slide.slide"].create({
                "name": item["name"], "channel_id": channel.id, "sequence": seq + i,
                "slide_category": "article", "is_published": True,
                "html_content": (
                    "<p>%s</p><p><a class='btn btn-primary' href='/eduagent/scorm/play/%s' target='_blank'>%s</a></p>"
                    "<p><small>%s</small></p>") % (
                    html_escape(item["name"]), sco.id, _("Launch interactive module"),
                    _("Your progress is saved automatically and this lesson is marked complete when you finish.")),
            })
            sco.slide_id = slide
        return {"type": "ir.actions.act_window", "res_model": "slide.channel", "res_id": channel.id,
                "view_mode": "form"}
