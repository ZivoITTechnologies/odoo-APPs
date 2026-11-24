# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase


class EduagentCase(TransactionCase):
    """Shared fixtures. All tests run on the built-in offline 'Demo' provider: no network calls."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_notrack=True))
        cls.demo_provider = cls.env.ref("zivoit_eduagent_ai.provider_demo")
        cls.env.company.eduagent_default_provider_id = cls.demo_provider
        cls.env.company.eduagent_require_oral_check = False
        Slide = cls.env["slide.slide"]
        cls.course = cls.env["slide.channel"].create({
            "name": "Python for Data Analysis",
            "description": "<p>Learn pandas, numpy and data cleaning for analytics jobs.</p>",
            "is_published": True,
        })
        Slide.create({"name": "Basics", "channel_id": cls.course.id, "is_category": True, "is_published": True})
        cls.slide_pandas = Slide.create({
            "name": "Pandas DataFrames", "channel_id": cls.course.id, "slide_category": "article",
            "is_published": True,
            "html_content": "<p>A DataFrame is a two-dimensional labeled table. Use groupby to aggregate rows.</p>",
            "question_ids": [(0, 0, {"question": "Which method aggregates rows?", "answer_ids": [
                (0, 0, {"text_value": "groupby", "is_correct": True}),
                (0, 0, {"text_value": "append", "is_correct": False})]})],
        })
        cls.slide_numpy = Slide.create({
            "name": "Numpy arrays", "channel_id": cls.course.id, "slide_category": "article", "is_published": True,
            "html_content": "<p>Numpy arrays are fast vectors used for numerical computing.</p>",
        })
        cls.env["eduagent.knowledge.chunk"].index_channel(cls.course)
        cls.learner = cls.env["res.partner"].create({"name": "Ravi Kumar", "email": "ravi.test@example.com"})
        cls.course._action_add_members(cls.learner)
        cls.enrolment = cls.env["slide.channel.partner"].search(
            [("channel_id", "=", cls.course.id), ("partner_id", "=", cls.learner.id)])
        cls.Conv = cls.env["eduagent.conversation"]

    def conversation(self, xmlid, partner=None, channel=None):
        agent = self.env.ref("zivoit_eduagent_ai.%s" % xmlid)
        return agent, self.Conv.create({
            "agent_id": agent.id,
            "partner_id": (partner or self.env.ref("base.public_partner")).id,
            "channel_id": channel.id if channel else False,
        })
