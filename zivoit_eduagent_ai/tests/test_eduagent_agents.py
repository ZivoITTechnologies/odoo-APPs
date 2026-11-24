# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import tagged

from .common import EduagentCase


@tagged("post_install", "-at_install", "eduagent")
class TestEduagentAgents(EduagentCase):

    def test_admissions_creates_scored_lead(self):
        agent, conv = self.conversation("agent_admissions")
        res = agent.run(conv, "I want a data analyst job. Email meera@example.com, phone +91 98765 43210, "
                              "please call me")
        self.assertTrue(res["reply"])
        lead = conv.lead_id
        self.assertTrue(lead)
        self.assertEqual(lead.email_from, "meera@example.com")
        self.assertGreater(lead.eduagent_score, 0)
        self.assertIn(self.course, lead.eduagent_channel_ids)
        self.assertTrue(lead.activity_ids, "counselling call activity expected")
        self.assertIn("meera@example.com", lead.eduagent_transcript)
        # second message updates the same lead instead of creating a duplicate
        agent.run(conv, "my email is meera@example.com")
        self.assertEqual(self.env["crm.lead"].search_count([("email_from", "=", "meera@example.com")]), 1)

    def test_disallowed_action_is_ignored(self):
        agent, conv = self.conversation("agent_tutor", self.learner, self.course)
        agent.allowed_actions = "escalate_to_human"
        fake = '{"reply": "x", "actions": [{"type": "create_ticket", "subject": "s"}]}'
        with patch.object(type(agent), "_mock_reply", lambda self, *a, **k: fake):
            agent.run(conv, "hello")
        self.assertFalse(self.env["eduagent.support.ticket"].search([("conversation_id", "=", conv.id)]))

    def test_tutor_cites_course_sources(self):
        agent, conv = self.conversation("agent_tutor", self.learner, self.course)
        res = agent.run(conv, "How do I aggregate rows with groupby?")
        self.assertTrue(res["sources"])
        self.assertEqual(res["sources"][0]["title"], "Pandas DataFrames")
        self.assertEqual(len(conv.message_ids_eduagent), 2)
        self.assertTrue(conv.message_ids_eduagent[-1].source_ids)

    def test_practice_logs_attempt_and_schedules_review(self):
        agent, conv = self.conversation("agent_practice", self.learner, self.course)
        agent.run(conv, "A DataFrame is a labelled 2D table and groupby aggregates rows")
        attempt = self.env["eduagent.practice.attempt"].search([("partner_id", "=", self.learner.id)])
        self.assertTrue(attempt)
        self.assertTrue(attempt.next_review)
        mastery = self.env["eduagent.practice.attempt"]._topic_mastery(self.learner)
        self.assertIn(attempt.topic, mastery)

    def test_support_ticket_and_human_takeover(self):
        agent, conv = self.conversation("agent_support", self.learner)
        agent.run(conv, "I have a problem, please open a ticket")
        ticket = self.env["eduagent.support.ticket"].search([("conversation_id", "=", conv.id)])
        self.assertTrue(ticket.reference.startswith("EDU-"))
        conv.action_take_over()
        res = agent.run(conv, "hello?")
        self.assertTrue(res.get("human"))
        conv.staff_reply = "A colleague here, checking now."
        conv.action_send_staff_reply()
        self.assertEqual(conv.message_ids_eduagent[-1].role, "human")

    def test_career_profile_and_matches(self):
        skill = self.env["eduagent.skill"]._get_or_create("pandas")
        self.course.eduagent_skill_ids = [(4, skill.id)]
        self.enrolment.write({"member_status": "completed", "completion": 100})
        job = self.env["eduagent.job.opening"].create({"name": "Junior Analyst", "skill_ids": [(4, skill.id)]})
        profile = self.env["eduagent.skill.profile"]._get_or_create(self.learner)
        profile.action_refresh_from_courses()
        self.assertIn(skill, profile.skill_ids)
        matches = profile.action_compute_matches()
        self.assertEqual(matches.job_id, job)
        self.assertEqual(matches.score, 100)
        profile.action_generate_resume()
        self.assertIn("Ravi Kumar", profile.resume_html)

    def test_public_partner_never_gets_learner_data(self):
        agent, conv = self.conversation("agent_fee")
        res = agent.run(conv, "where do I pay?")
        self.assertIn("sign in", res["reply"].lower())

    def test_lead_capture_works_with_pii_masking(self):
        """Regression: with masking on, the LLM only sees [email]; the module must still capture it."""
        self.env.company.eduagent_pii_masking = True
        agent, conv = self.conversation("agent_admissions")
        seen = {}

        def fake_chat(provider, system, messages, **kw):
            seen["text"] = messages[-1]["content"]
            return {"text": '{"reply": "Thanks!", "actions": [{"type": "capture_lead", "email": "[email]", '
                            '"name": "Meera"}]}', "input_tokens": 10, "output_tokens": 5, "model": "t"}

        with patch.object(type(self.env["eduagent.provider"]), "_chat", fake_chat):
            agent.run(conv, "I'm Meera, reach me at meera.masked@example.com or +91 98480 22145")
        self.assertNotIn("meera.masked@example.com", seen["text"], "email must not reach the provider")
        self.assertEqual(conv.lead_id.email_from, "meera.masked@example.com")
        self.assertEqual(conv.lead_id.phone, "+91 98480 22145")

    def test_merge_contact_details_injects_capture(self):
        Agent = type(self.env["eduagent.agent"])
        actions = Agent._merge_contact_details([{"type": "recommend_courses"}], "mail x@y.io")
        self.assertEqual(actions[0], {"type": "capture_lead", "email": "x@y.io"})
        self.assertEqual(Agent._merge_contact_details([], "no contact"), [])
