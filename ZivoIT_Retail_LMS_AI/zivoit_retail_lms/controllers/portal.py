# -*- coding: utf-8 -*-
from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal


class ZlmsPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'zlms_assignment_count' in counters:
            learner = request.env['zlms.learner']._get_current_learner()
            values['zlms_assignment_count'] = request.env['zlms.assignment'].sudo().search_count([
                ('learner_id', '=', learner.id), ('state', 'in', ('assigned', 'in_progress'))]) if learner else 0
        return values

    # ------------------------------------------------------------------
    def _zlms_learner(self):
        return request.env['zlms.learner']._get_current_learner()

    def _zlms_render(self, template, values):
        values.setdefault('page_name', 'zlms_learning')
        return request.render(template, values)

    def _zlms_no_profile(self):
        return self._zlms_render('zivoit_retail_lms.portal_zlms_no_profile', {})

    @http.route(['/my/learning'], type='http', auth='user', website=True)
    def zlms_home(self, **kw):
        learner = self._zlms_learner()
        if not learner:
            return self._zlms_no_profile()
        learner.action_sync_progress()
        Card = request.env['zlms.boost.card'].sudo()
        Card._seed_for_learners(learner)
        limit = int(request.env['ir.config_parameter'].sudo().get_param('zivoit_retail_lms.boost_daily_questions', 5) or 5)
        due = Card._get_due_cards(learner, limit=limit)
        store_team = request.env['zlms.learner'].sudo().search(
            [('store_id', '=', learner.store_id.id), ('active', '=', True)], order='points desc', limit=5) \
            if learner.store_id else learner
        assignments = learner.assignment_ids.filtered(lambda a: a.state in ('assigned', 'in_progress', 'completed'))
        pending_news = request.env['zlms.bulletin.ack'].sudo().search(
            [('learner_id', '=', learner.id), ('state', '!=', 'acknowledged'), ('bulletin_id.state', '=', 'published')])
        my_sessions = request.env['zlms.training.attendee'].sudo().search(
            [('learner_id', '=', learner.id), ('state', '=', 'registered'),
             ('start_datetime', '>=', fields.Datetime.now())], order='start_datetime', limit=3)
        return self._zlms_render('zivoit_retail_lms.portal_zlms_home', {
            'learner': learner,
            'open_assignments': assignments.filtered(lambda a: a.state != 'completed').sorted('due_date'),
            'done_assignments': assignments.filtered(lambda a: a.state == 'completed'),
            'boost_due': len(due),
            'certificates': learner.certificate_ids.filtered(lambda c: c.state != 'revoked'),
            'store_team': store_team,
            'pending_news': pending_news,
            'my_sessions': my_sessions,
            'badges': learner.badge_award_ids,
            'challenge': request.env['zlms.store'].sudo().get_store_challenge(limit=5),
        })

    # ------------------------------------------------------------------
    # Daily Boost
    # ------------------------------------------------------------------
    @http.route(['/my/learning/boost'], type='http', auth='user', website=True)
    def zlms_boost(self, **kw):
        learner = self._zlms_learner()
        if not learner:
            return self._zlms_no_profile()
        Card = request.env['zlms.boost.card'].sudo()
        Card._seed_for_learners(learner)
        card = Card._get_due_cards(learner, limit=1)
        return self._zlms_render('zivoit_retail_lms.portal_zlms_boost', {
            'learner': learner, 'card': card, 'feedback': None,
            'answers': card.question_id.answer_ids.sorted('sequence') if card else [],
        })

    @http.route(['/my/learning/boost/answer'], type='http', auth='user', website=True, methods=['POST'])
    def zlms_boost_answer(self, card_id=None, answer_id=None, confident=None, **kw):
        learner = self._zlms_learner()
        card = request.env['zlms.boost.card'].sudo().browse(int(card_id or 0)).exists()
        if not learner or not card or card.learner_id != learner or not answer_id:
            return request.redirect('/my/learning/boost')
        feedback = card.answer(int(answer_id), confident=confident != '0')
        return self._zlms_render('zivoit_retail_lms.portal_zlms_boost', {
            'learner': learner, 'card': card, 'feedback': feedback, 'chosen': int(answer_id),
            'answers': card.question_id.answer_ids.sorted('sequence'),
        })

    # ------------------------------------------------------------------
    # Ask the Expert
    # ------------------------------------------------------------------
    @http.route(['/my/learning/ask'], type='http', auth='user', website=True, methods=['GET', 'POST'])
    def zlms_ask(self, question=None, **kw):
        learner = self._zlms_learner()
        if not learner:
            return self._zlms_no_profile()
        result = None
        if question and request.httprequest.method == 'POST':
            result = request.env['zlms.ask.expert.wizard'].sudo().answer_question(
                question[:1000], learner.preferred_language or 'English')
            result['cards'] = request.env['zlms.product.card'].sudo().browse(result['card_ids'])
        return self._zlms_render('zivoit_retail_lms.portal_zlms_ask', {
            'learner': learner, 'question': question or '', 'result': result})

    # ------------------------------------------------------------------
    # Role-play coach
    # ------------------------------------------------------------------
    @http.route(['/my/learning/roleplay'], type='http', auth='user', website=True)
    def zlms_roleplay_list(self, **kw):
        learner = self._zlms_learner()
        if not learner:
            return self._zlms_no_profile()
        scenarios = request.env['zlms.roleplay.scenario'].sudo().search([])
        return self._zlms_render('zivoit_retail_lms.portal_zlms_roleplay_list', {
            'learner': learner, 'scenarios': scenarios,
            'sessions': learner.roleplay_session_ids[:10],
        })

    @http.route(['/my/learning/roleplay/start/<int:scenario_id>'], type='http', auth='user', website=True,
                methods=['POST'])
    def zlms_roleplay_start(self, scenario_id, **kw):
        learner = self._zlms_learner()
        scenario = request.env['zlms.roleplay.scenario'].sudo().browse(scenario_id).exists()
        if not learner or not scenario:
            return request.redirect('/my/learning/roleplay')
        session = request.env['zlms.roleplay.session'].sudo().create(
            {'scenario_id': scenario.id, 'learner_id': learner.id})
        session.action_start()
        return request.redirect('/my/learning/roleplay/%s' % session.id)

    def _zlms_get_session(self, session_id):
        learner = self._zlms_learner()
        session = request.env['zlms.roleplay.session'].sudo().browse(session_id).exists()
        if not learner or not session or session.learner_id != learner:
            return None
        return session

    @http.route(['/my/learning/roleplay/<int:session_id>'], type='http', auth='user', website=True,
                methods=['GET', 'POST'])
    def zlms_roleplay_session(self, session_id, message=None, evaluate=None, **kw):
        session = self._zlms_get_session(session_id)
        if not session:
            return request.redirect('/my/learning/roleplay')
        if request.httprequest.method == 'POST' and session.state == 'ongoing':
            if evaluate:
                session.action_evaluate()
            elif message and message.strip():
                session.send_message(message)
        return self._zlms_render('zivoit_retail_lms.portal_zlms_roleplay_session', {
            'learner': session.learner_id, 'session': session})

    # ------------------------------------------------------------------
    # Store bulletins
    # ------------------------------------------------------------------
    @http.route(['/my/learning/news'], type='http', auth='user', website=True)
    def zlms_news(self, **kw):
        learner = self._zlms_learner()
        if not learner:
            return self._zlms_no_profile()
        acks = request.env['zlms.bulletin.ack'].sudo().search(
            [('learner_id', '=', learner.id), ('bulletin_id.state', '=', 'published')])
        acks = acks.sorted(lambda a: (a.state == 'acknowledged', a.bulletin_id.priority != 'urgent',
                                      -(a.bulletin_id.id)))
        return self._zlms_render('zivoit_retail_lms.portal_zlms_news', {'learner': learner, 'acks': acks})

    def _zlms_get_ack(self, ack_id):
        learner = self._zlms_learner()
        ack = request.env['zlms.bulletin.ack'].sudo().browse(ack_id).exists()
        if not learner or not ack or ack.learner_id != learner or ack.bulletin_id.state != 'published':
            return None
        return ack

    @http.route(['/my/learning/news/<int:ack_id>'], type='http', auth='user', website=True, methods=['GET', 'POST'])
    def zlms_news_item(self, ack_id, acknowledge=None, **kw):
        ack = self._zlms_get_ack(ack_id)
        if not ack:
            return request.redirect('/my/learning/news')
        ack.action_mark_read()
        if request.httprequest.method == 'POST' and acknowledge:
            ack.action_acknowledge()
        return self._zlms_render('zivoit_retail_lms.portal_zlms_news_item', {'learner': ack.learner_id, 'ack': ack})

    # ------------------------------------------------------------------
    # Instructor-led sessions
    # ------------------------------------------------------------------
    @http.route(['/my/learning/sessions'], type='http', auth='user', website=True, methods=['GET', 'POST'])
    def zlms_sessions(self, session_id=None, action=None, rating=None, comment=None, **kw):
        learner = self._zlms_learner()
        if not learner:
            return self._zlms_no_profile()
        Session = request.env['zlms.training.session'].sudo()
        error = None
        if request.httprequest.method == 'POST' and session_id:
            session = Session.browse(int(session_id)).exists()
            mine = session.attendee_ids.filtered(lambda a: a.learner_id == learner) if session else None
            try:
                if session and action == 'register' and self._zlms_session_open_to(session, learner):
                    session.register(learner)
                elif mine and action == 'cancel' and session.state == 'planned':
                    mine.state = 'cancelled'
                elif mine and action == 'feedback' and mine.state == 'attended' and rating in ('1', '2', '3', '4', '5'):
                    mine.write({'feedback_rating': rating, 'feedback_comment': (comment or '')[:1000]})
            except UserError as exc:
                error = str(exc)
        now = fields.Datetime.now()
        upcoming = Session.search([('state', '=', 'planned'), ('start_datetime', '>=', now)], order='start_datetime')
        upcoming = upcoming.filtered(lambda s: self._zlms_session_open_to(s, learner))
        attendances = request.env['zlms.training.attendee'].sudo().search(
            [('learner_id', '=', learner.id), ('state', '!=', 'cancelled')], order='start_datetime desc')
        return self._zlms_render('zivoit_retail_lms.portal_zlms_sessions', {
            'learner': learner, 'upcoming': upcoming, 'attendances': attendances,
            'registered': attendances.filtered(lambda a: a.state == 'registered').session_id, 'error': error})

    @staticmethod
    def _zlms_session_open_to(session, learner):
        if session.store_id and session.store_id != learner.store_id and not session.region_id:
            return False
        if session.region_id and session.region_id != learner.region_id:
            return False
        return True

    # ------------------------------------------------------------------
    # Certificates
    # ------------------------------------------------------------------
    @http.route(['/my/learning/certificate/<int:cert_id>'], type='http', auth='user', website=True)
    def zlms_certificate(self, cert_id, **kw):
        learner = self._zlms_learner()
        cert = request.env['zlms.certificate'].sudo().browse(cert_id).exists()
        if not learner or not cert or cert.learner_id != learner:
            return request.redirect('/my/learning')
        pdf, _ctype = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'zivoit_retail_lms.action_report_zlms_certificate', cert.ids)
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Disposition', 'attachment; filename="%s.pdf"' % cert.name.replace('/', '-')),
        ])
