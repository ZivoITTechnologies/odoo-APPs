# -*- coding: utf-8 -*-
"""SCORM 1.2 / 2004 package import and tracking."""
import base64
import io
import posixpath
import zipfile
from xml.etree import ElementTree

from odoo import _, api, fields, models
from odoo.exceptions import UserError


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class EduagentScormPackage(models.Model):
    _name = "eduagent.scorm.package"
    _description = "SCORM Package"

    name = fields.Char(required=True)
    channel_id = fields.Many2one("slide.channel", string="Course", ondelete="cascade")
    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="restrict")
    version = fields.Char(readonly=True)
    item_ids = fields.One2many("eduagent.scorm.item", "package_id", string="SCOs")
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    def _zip(self):
        self.ensure_one()
        data = self.attachment_id.sudo().raw
        return zipfile.ZipFile(io.BytesIO(data))

    def _read_file(self, path):
        self.ensure_one()
        path = posixpath.normpath(path).lstrip("/")
        if path.startswith("..") or "\\" in path:
            return None
        try:
            with self._zip() as zf:
                return zf.read(path)
        except KeyError:
            return None

    @api.model
    def parse_manifest(self, raw_zip):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_zip))
        except zipfile.BadZipFile as exc:
            raise UserError(_("Not a valid SCORM zip file.")) from exc
        names = zf.namelist()
        manifest_name = next((n for n in names if n.lower().endswith("imsmanifest.xml")), None)
        if not manifest_name:
            raise UserError(_("imsmanifest.xml not found in the package."))
        prefix = manifest_name[: -len("imsmanifest.xml")]
        root = ElementTree.fromstring(zf.read(manifest_name))
        version = "1.2"
        for el in root.iter():
            if _local(el.tag) == "schemaversion" and el.text:
                version = el.text.strip()
        resources = {}
        for el in root.iter():
            if _local(el.tag) == "resource":
                rid = el.get("identifier")
                href = el.get("href")
                if rid and href:
                    resources[rid] = prefix + href
        title = None
        items = []
        for org in root.iter():
            if _local(org.tag) != "organization":
                continue
            for child in org:
                if _local(child.tag) == "title" and child.text and not title:
                    title = child.text.strip()
            for item in org.iter():
                if _local(item.tag) != "item":
                    continue
                ref = item.get("identifierref")
                item_title = next((c.text.strip() for c in item if _local(c.tag) == "title" and c.text), ref)
                if ref and ref in resources:
                    href = resources[ref]
                    params = item.get("parameters") or ""
                    items.append({"name": item_title or href, "launch_path": href + params})
            break
        if not items and resources:
            items = [{"name": title or "SCORM", "launch_path": next(iter(resources.values()))}]
        return {"title": title or _("SCORM course"), "version": version, "items": items}


class EduagentScormItem(models.Model):
    _name = "eduagent.scorm.item"
    _description = "SCORM SCO"
    _order = "sequence, id"

    package_id = fields.Many2one("eduagent.scorm.package", required=True, ondelete="cascade")
    sequence = fields.Integer()
    name = fields.Char(required=True)
    launch_path = fields.Char(required=True)
    slide_id = fields.Many2one("slide.slide", string="Lesson", ondelete="set null")


class EduagentScormTracking(models.Model):
    _name = "eduagent.scorm.tracking"
    _description = "SCORM Learner Tracking"
    _order = "write_date desc"

    item_id = fields.Many2one("eduagent.scorm.item", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one("res.partner", required=True, ondelete="cascade", index=True)
    lesson_status = fields.Char()
    score_raw = fields.Float()
    suspend_data = fields.Text()
    lesson_location = fields.Char()
    total_time = fields.Char()
    cmi_json = fields.Text()

    _uniq = models.Constraint("UNIQUE(item_id, partner_id)", "One tracking row per learner and SCO.")
