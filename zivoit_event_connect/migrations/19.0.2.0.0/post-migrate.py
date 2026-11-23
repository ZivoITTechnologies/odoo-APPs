# -*- coding: utf-8 -*-
"""19.0.2.0.0 (AI release): the invitation template (noupdate) learns the AI-written subject/body."""
from markupsafe import Markup

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    template = env.ref('zivoit_event_connect.mail_template_event_invitation', raise_if_not_found=False)
    if not template:
        return
    template.subject = "{{ object.event_id.zivo_invite_subject or ('You are invited: ' + object.event_id.name) }}"
    old = ('<p>You have been personally invited to <strong t-out="object.event_id.name or \'\'"/>.')
    new = ('<p t-if="object.event_id.zivo_invite_body" style="white-space:pre-line;" '
           't-out="object.event_id.zivo_invite_body"/>\n        <p t-else="">You have been personally invited to '
           '<strong t-out="object.event_id.name or \'\'"/>.')
    body = str(template.body_html or '')
    if old in body and 'zivo_invite_body' not in body:
        template.body_html = Markup(body.replace(old, new))
