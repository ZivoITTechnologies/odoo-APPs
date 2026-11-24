# -*- coding: utf-8 -*-
"""Course Builder agent: turns a topic + source material into a complete eLearning course
(sections, article lessons, quizzes) as an unpublished draft for human review."""
import base64
import io
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext, html_escape

SYSTEM = """You are an expert instructional designer. Build a course outline with real teaching content.
Answer JSON exactly like:
{"title":"","description":"","skills":[""],
 "sections":[{"title":"","lessons":[{"title":"","duration_minutes":10,
   "content_html":"<h3>..</h3><p>..</p><ul><li>..</li></ul>",
   "questions":[{"question":"","answers":[{"text":"","correct":true,"comment":""}]}]}]}]}
Rules: content_html uses only h3, h4, p, ul, ol, li, b, i, code, pre, blockquote, table tags.
Each lesson 250-500 words with an example and a 'Key takeaways' list.
Each question has 3-4 answers and exactly one correct. Use the SOURCE MATERIAL when given and do not
invent facts beyond it; when no source is given use well-established knowledge only."""


class EduagentCourseBuilderWizard(models.TransientModel):
    _name = "eduagent.course.builder.wizard"
    _description = "AI Course Builder"

    topic = fields.Char(required=True)
    audience = fields.Char(default="Working professionals")
    level = fields.Selection([("beginner", "Beginner"), ("intermediate", "Intermediate"),
                              ("advanced", "Advanced")], default="beginner", required=True)
    language = fields.Char(default="English")
    section_count = fields.Integer(string="Sections", default=3)
    lessons_per_section = fields.Integer(default=3)
    include_quiz = fields.Boolean(string="Generate Quiz Questions", default=True)
    questions_per_lesson = fields.Integer(default=2)
    source_text = fields.Text(string="Source Material (paste)")
    source_file = fields.Binary(string="Source File (.txt, .md, .html, .pdf)")
    source_filename = fields.Char()
    channel_id = fields.Many2one("slide.channel", string="Add to Existing Course")
    state = fields.Selection([("input", "Input"), ("done", "Done")], default="input")
    result_channel_id = fields.Many2one("slide.channel", readonly=True)
    preview = fields.Html(readonly=True)

    def _source(self):
        text = self.source_text or ""
        if self.source_file:
            raw = base64.b64decode(self.source_file)
            name = (self.source_filename or "").lower()
            if name.endswith(".pdf"):
                try:
                    import pypdf  # dependency of Odoo itself
                    reader = pypdf.PdfReader(io.BytesIO(raw))
                    text += "\n".join((p.extract_text() or "") for p in reader.pages)
                except Exception as exc:  # pylint: disable=broad-except
                    raise UserError(_("Could not read the PDF: %s", exc)) from exc
            elif name.endswith((".html", ".htm")):
                text += html2plaintext(raw.decode("utf-8", "ignore"))
            else:
                text += raw.decode("utf-8", "ignore")
        return text[:60000]

    def _mock_outline(self):
        sections = []
        for s in range(max(self.section_count, 1)):
            lessons = []
            for l in range(max(self.lessons_per_section, 1)):
                title = _("%(t)s - part %(s)s.%(l)s", t=self.topic, s=s + 1, l=l + 1)
                lessons.append({
                    "title": title, "duration_minutes": 10,
                    "content_html": "<h3>%s</h3><p>%s</p><h4>Key takeaways</h4><ul><li>%s</li></ul>" % (
                        html_escape(title), _("Draft lesson generated in demo mode. Connect an AI provider "
                                              "to generate full teaching content."), html_escape(self.topic)),
                    "questions": [{"question": _("What is this lesson about?"), "answers": [
                        {"text": self.topic, "correct": True}, {"text": _("Something else"), "correct": False},
                        {"text": _("Not sure"), "correct": False}]}] if self.include_quiz else [],
                })
            sections.append({"title": _("Module %s", s + 1), "lessons": lessons})
        return {"title": self.topic, "description": _("A %s course on %s.", self.level, self.topic),
                "skills": [self.topic], "sections": sections}

    def action_generate(self):
        self.ensure_one()
        agent = self.env.ref("zivoit_eduagent_ai.agent_course_builder", raise_if_not_found=False)
        source = self._source()
        brief = {
            "topic": self.topic, "audience": self.audience, "level": self.level, "language": self.language,
            "sections": self.section_count, "lessons_per_section": self.lessons_per_section,
            "questions_per_lesson": self.questions_per_lesson if self.include_quiz else 0,
        }
        prompt = "BRIEF:\n%s\n\nSOURCE MATERIAL:\n%s" % (json.dumps(brief), source or "(none)")
        res = self.env["eduagent.llm"].complete(
            SYSTEM, [{"role": "user", "content": prompt}], agent=agent, json_mode=True,
            purpose="Course Builder", max_tokens=8000, mock_response=json.dumps(self._mock_outline()))
        if not res["ok"]:
            raise UserError(res["text"])
        outline = res["data"]
        if not outline.get("sections"):
            raise UserError(_("The AI did not return a usable outline. Try again or reduce the size."))
        channel = self._create_course(outline)
        self.env["eduagent.knowledge.chunk"].index_channel(channel)
        preview = "".join("<h4>%s</h4><ul>%s</ul>" % (
            html_escape(s.get("title", "")),
            "".join("<li>%s</li>" % html_escape(l.get("title", "")) for l in s.get("lessons", [])))
            for s in outline["sections"])
        self.write({"state": "done", "result_channel_id": channel.id, "preview": preview})
        return {"type": "ir.actions.act_window", "name": _("AI Course Builder"), "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}

    def _create_course(self, outline):
        Slide = self.env["slide.slide"]
        Skill = self.env["eduagent.skill"]
        channel = self.channel_id
        if not channel:
            channel = self.env["slide.channel"].create({
                "name": outline.get("title") or self.topic,
                "description": "<p>%s</p>" % html_escape(outline.get("description") or ""),
                "channel_type": "training",
                "is_published": False,
                "eduagent_skill_ids": [(4, Skill._get_or_create(s).id) for s in (outline.get("skills") or [])
                                       if isinstance(s, str) and s.strip()],
            })
        seq = max(channel.slide_ids.mapped("sequence") or [0]) + 1
        for section in outline.get("sections", []):
            Slide.create({"name": section.get("title") or _("Section"), "channel_id": channel.id,
                          "is_category": True, "sequence": seq, "is_published": True})
            seq += 1
            for lesson in section.get("lessons", []):
                try:
                    minutes = float(lesson.get("duration_minutes") or 10)
                except (TypeError, ValueError):
                    minutes = 10.0
                questions = []
                for q in lesson.get("questions", []) or []:
                    answers = [(0, 0, {"text_value": (a.get("text") or "-")[:250], "is_correct": bool(a.get("correct")),
                                       "comment": a.get("comment") or False})
                               for a in (q.get("answers") or []) if isinstance(a, dict)]
                    if len(answers) >= 2 and any(a[2]["is_correct"] for a in answers):
                        questions.append((0, 0, {"question": (q.get("question") or "?")[:250],
                                                 "answer_ids": answers}))
                Slide.create({
                    "name": lesson.get("title") or _("Lesson"), "channel_id": channel.id, "sequence": seq,
                    "slide_category": "article", "html_content": lesson.get("content_html") or "<p></p>",
                    "completion_time": minutes / 60.0, "is_published": True,
                    "question_ids": questions,
                })
                seq += 1
        channel.message_post(body=_("Draft generated by the AI Course Builder. Review every lesson before "
                                    "publishing."))
        return channel

    def action_open_course(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "slide.channel",
                "res_id": self.result_channel_id.id, "view_mode": "form"}
