# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
from odoo.tools import email_normalize

from odoo.addons.survey.controllers.main import Survey


class ZivoSurveyController(Survey):

    def _zivo_access(self, survey_token, answer_token):
        access_data = self._get_access_data(survey_token, answer_token, ensure_token=True)
        if access_data['validity_code'] is not True:
            return None, None
        survey_sudo, answer_sudo = access_data['survey_sudo'], access_data['answer_sudo']
        if not survey_sudo.zivo_allow_save_later or answer_sudo.state == 'done' or answer_sudo.is_session_answer:
            return None, None
        return survey_sudo, answer_sudo

    @http.route('/survey/zivo/save_later/<string:survey_token>/<string:answer_token>', type='jsonrpc',
                auth='public', website=True)
    def zivo_survey_save_later(self, survey_token, answer_token, **post):
        """Save the answers of the current page WITHOUT mandatory validation and park the
        respondent on that page so the resume link reopens exactly where they left."""
        survey_sudo, answer_sudo = self._zivo_access(survey_token, answer_token)
        if not survey_sudo:
            return {'error': 'unauthorized'}
        if answer_sudo.state == 'new':
            answer_sudo._mark_in_progress()
        questions, page_or_question_id = survey_sudo._get_survey_questions(
            answer=answer_sudo, page_id=post.get('page_id'), question_id=post.get('question_id'))
        inactive = answer_sudo._get_inactive_conditional_questions()
        saved = 0
        for question in questions:
            if question in inactive or str(question.id) not in post:
                continue
            answer, comment = self._extract_comment_from_answers(question, post.get(str(question.id)))
            errors = question.validate_question(answer, comment)
            if errors.get(question.id):
                continue  # keep partial answers that are valid; invalid ones are simply not saved
            answer_sudo._save_lines(question, answer, comment, overwrite_existing=True)
            saved += 1
        # Resume on the page currently displayed: last_displayed_page_id must point to the previous one
        if survey_sudo.questions_layout != 'one_page' and page_or_question_id:
            previous = survey_sudo._get_next_page_or_question(answer_sudo, page_or_question_id, go_back=True)
            answer_sudo.last_displayed_page_id = previous.id if previous else False
        answer_sudo._zivo_mark_saved()
        return {
            'saved_questions': saved,
            'url': '/survey/zivo/saved/%s/%s' % (survey_sudo.access_token, answer_sudo.access_token),
        }

    @http.route('/survey/zivo/saved/<string:survey_token>/<string:answer_token>', type='http',
                auth='public', website=True, sitemap=False)
    def zivo_survey_saved(self, survey_token, answer_token, sent=None, error=None, **kw):
        survey_sudo, answer_sudo = self._zivo_access(survey_token, answer_token)
        if not survey_sudo:
            return request.redirect('/survey/%s' % survey_token)
        return request.render('zivoit_event_connect.survey_saved_page', {
            'survey': survey_sudo,
            'zivo_answer': answer_sudo,
            'sent': sent,
            'error': error,
            'default_email': answer_sudo.email or (answer_sudo.partner_id.email if answer_sudo.partner_id else ''),
        })

    @http.route('/survey/zivo/send_resume/<string:survey_token>/<string:answer_token>', type='http',
                auth='public', website=True, methods=['POST'], sitemap=False)
    def zivo_survey_send_resume(self, survey_token, answer_token, email=None, **kw):
        survey_sudo, answer_sudo = self._zivo_access(survey_token, answer_token)
        if not survey_sudo:
            return request.redirect('/survey/%s' % survey_token)
        email = email_normalize(email or '')
        base = '/survey/zivo/saved/%s/%s' % (survey_token, answer_token)
        if not email:
            return request.redirect(base + '?error=email')
        answer_sudo.zivo_resume_email = email
        template = request.env.ref('zivoit_event_connect.mail_template_survey_resume').sudo()
        template.send_mail(answer_sudo.id, force_send=True, email_values={'email_to': email},
                           email_layout_xmlid='mail.mail_notification_light')
        return request.redirect(base + '?sent=1')

    @http.route(['/survey-resume'], type='http', auth='public', website=True, sitemap=False,
                methods=['GET', 'POST'])
    def zivo_survey_resume(self, code=None, **kw):
        error = False
        if code:
            answer_sudo = request.env['survey.user_input'].sudo()._zivo_find_by_code(code)
            if answer_sudo:
                return request.redirect('/survey/%s/%s' % (answer_sudo.survey_id.access_token,
                                                            answer_sudo.access_token))
            error = True
        return request.render('zivoit_event_connect.survey_resume_page', {'code': code or '', 'error': error})
