# -*- coding: utf-8 -*-
import json

from odoo import _, api, fields, models
from odoo.tools import html2plaintext


class EduagentSkill(models.Model):
    _name = "eduagent.skill"
    _description = "EduAgent Skill"
    _order = "name"

    name = fields.Char(required=True)

    _name_unique = models.Constraint("UNIQUE(name)", "Skill names must be unique.")

    @api.model
    def _get_or_create(self, name):
        name = (name or "").strip()[:80]
        skill = self.search([("name", "=ilike", name)], limit=1)
        return skill or self.create({"name": name})


class EduagentSkillProfile(models.Model):
    _name = "eduagent.skill.profile"
    _description = "Learner Skill Profile"
    _inherit = ["mail.thread"]

    partner_id = fields.Many2one("res.partner", required=True, ondelete="cascade", index=True)
    name = fields.Char(related="partner_id.name", store=True)
    headline = fields.Char()
    target_role = fields.Char()
    skill_ids = fields.Many2many("eduagent.skill", string="Skills")
    completed_channel_ids = fields.Many2many("slide.channel", string="Completed Courses")
    credential_ids = fields.One2many("eduagent.credential", compute="_compute_credentials")
    resume_html = fields.Html(string="AI Resume")
    open_to_work = fields.Boolean(default=True)
    match_ids = fields.One2many("eduagent.job.match", "profile_id")

    _partner_unique = models.Constraint("UNIQUE(partner_id)", "One skill profile per learner.")

    def _compute_credentials(self):
        for rec in self:
            rec.credential_ids = self.env["eduagent.credential"].search(
                [("partner_id", "=", rec.partner_id.id), ("state", "=", "valid")])

    @api.model
    def _get_or_create(self, partner):
        profile = self.search([("partner_id", "=", partner.id)], limit=1)
        return profile or self.create({"partner_id": partner.id})

    def action_refresh_from_courses(self):
        for rec in self:
            done = self.env["slide.channel.partner"].sudo().search([
                ("partner_id", "=", rec.partner_id.id), ("member_status", "=", "completed")])
            channels = done.mapped("channel_id")
            skills = channels.mapped("eduagent_skill_ids")
            rec.write({"completed_channel_ids": [(6, 0, channels.ids)],
                       "skill_ids": [(4, s.id) for s in skills]})
        return True

    def action_generate_resume(self):
        agent = self.env.ref("zivoit_eduagent_ai.agent_career", raise_if_not_found=False)
        for rec in self:
            facts = {
                "name": rec.partner_id.name, "headline": rec.headline, "target_role": rec.target_role,
                "skills": rec.skill_ids.mapped("name"),
                "completed_courses": rec.completed_channel_ids.mapped("name"),
                "credentials": rec.credential_ids.mapped("code"),
            }
            mock = "<h3>%s</h3><p>%s</p><h4>Skills</h4><p>%s</p><h4>Training</h4><p>%s</p>" % (
                rec.partner_id.name, rec.headline or rec.target_role or "",
                ", ".join(facts["skills"]) or "-", ", ".join(facts["completed_courses"]) or "-")
            res = self.env["eduagent.llm"].complete(
                "Write a concise one-page resume in clean HTML (h3, h4, p, ul only) using ONLY these facts. "
                "Do not invent employers, dates or degrees.",
                [{"role": "user", "content": json.dumps(facts)}], agent=agent, purpose="Resume",
                partner=rec.partner_id, mock_response=mock)
            rec.resume_html = res["text"] if res["ok"] else mock
        return True

    def action_compute_matches(self):
        Match = self.env["eduagent.job.match"]
        result = Match.browse()
        jobs = self.env["eduagent.job.opening"].search([("active", "=", True)])
        for rec in self:
            mine = set(rec.skill_ids.ids)
            for job in jobs:
                need = set(job.skill_ids.ids)
                score = int(round(100.0 * len(mine & need) / len(need))) if need else 0
                match = Match.search([("profile_id", "=", rec.id), ("job_id", "=", job.id)], limit=1)
                if score <= 0:
                    match.filtered(lambda m: m.state == "suggested").unlink()
                    continue
                if match:
                    match.score = score
                else:
                    match = Match.create({"profile_id": rec.id, "job_id": job.id, "score": score})
                result |= match
        return result


class EduagentJobOpening(models.Model):
    _name = "eduagent.job.opening"
    _description = "Employer Job Opening"
    _inherit = ["mail.thread"]

    name = fields.Char(string="Role", required=True)
    employer_id = fields.Many2one("res.partner", string="Employer")
    location = fields.Char()
    description = fields.Html()
    skill_ids = fields.Many2many("eduagent.skill", string="Required Skills")
    apply_url = fields.Char()
    active = fields.Boolean(default=True)
    match_ids = fields.One2many("eduagent.job.match", "job_id")
    match_count = fields.Integer(compute="_compute_match_count")

    def _compute_match_count(self):
        for rec in self:
            rec.match_count = len(rec.match_ids)

    def action_extract_skills(self):
        """Use AI to read the job description and set required skills."""
        Skill = self.env["eduagent.skill"]
        for rec in self:
            text = html2plaintext(rec.description or "") or rec.name
            res = self.env["eduagent.llm"].complete(
                'Extract the required skills from this job post. Answer JSON {"skills": ["..."]}, '
                "max 12 short skill names.",
                [{"role": "user", "content": text[:6000]}], tier="fast", json_mode=True,
                purpose="Job skill extraction", mock_response=json.dumps({"skills": [rec.name]}))
            names = (res.get("data") or {}).get("skills") or []
            rec.skill_ids = [(4, Skill._get_or_create(n).id) for n in names if isinstance(n, str) and n.strip()]
        return True


class EduagentJobMatch(models.Model):
    _name = "eduagent.job.match"
    _description = "Learner - Job Match"
    _order = "score desc"

    profile_id = fields.Many2one("eduagent.skill.profile", required=True, ondelete="cascade")
    partner_id = fields.Many2one(related="profile_id.partner_id", store=True)
    job_id = fields.Many2one("eduagent.job.opening", required=True, ondelete="cascade")
    employer_id = fields.Many2one(related="job_id.employer_id", store=True)
    score = fields.Integer(string="Match %")
    state = fields.Selection([("suggested", "Suggested"), ("shared", "Shared with Employer"),
                              ("applied", "Applied"), ("hired", "Hired"), ("rejected", "Not selected")],
                             default="suggested")

    def action_share(self):
        self.write({"state": "shared"})
