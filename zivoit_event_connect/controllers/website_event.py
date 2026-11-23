# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request

from odoo.addons.website_event.controllers.main import WebsiteEventController

from ..models.event_event import SESSION_INVITE_KEY


class ZivoWebsiteEventController(WebsiteEventController):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _zivo_denied(self, event):
        values = {'event': event.sudo(), 'main_object': event.sudo()}
        return request.render('zivoit_event_connect.event_access_denied', values, status=403)

    def _zivo_allowed(self, event):
        return event.sudo().with_user(request.env.user)._zivo_has_access()

    # ------------------------------------------------------------------
    # Listing: department filter (/event?department=hr)
    # ------------------------------------------------------------------
    def _get_events_search_options(self, slug_tags, **post):
        options = super()._get_events_search_options(slug_tags, **post)
        options['zivo_department'] = post.get('department')
        return options

    # ------------------------------------------------------------------
    # Invitation link
    # ------------------------------------------------------------------
    @http.route(['/event/invite/<string:token>'], type='http', auth='public', website=True, sitemap=False)
    def zivo_event_invitation(self, token, **kwargs):
        invitation = request.env['zivo.event.invitation'].sudo().search([('token', '=', token)], limit=1)
        if not invitation or invitation.state == 'revoked':
            return request.render('zivoit_event_connect.event_invitation_invalid', {}, status=404)
        tokens = list(request.session.get(SESSION_INVITE_KEY) or [])
        if token not in tokens:
            tokens.append(token)
            request.session[SESSION_INVITE_KEY] = tokens[-50:]
        invitation._mark_opened()
        return request.redirect('/event/%s/register' % invitation.event_id.id)

    # ------------------------------------------------------------------
    # Guarded event routes
    # ------------------------------------------------------------------
    @http.route()
    def event(self, event, **post):
        if not self._zivo_allowed(event):
            return self._zivo_denied(event)
        return super().event(event, **post)

    @http.route()
    def event_page(self, event, page, **post):
        if not self._zivo_allowed(event):
            return self._zivo_denied(event)
        return super().event_page(event, page, **post)

    @http.route()
    def event_register(self, event, **post):
        if not self._zivo_allowed(event):
            return self._zivo_denied(event)
        return super().event_register(event, **post)

    @http.route()
    def registration_new(self, event, **post):
        if not self._zivo_allowed(event):
            return False
        return super().registration_new(event, **post)

    @http.route()
    def registration_confirm(self, event, **post):
        if not self._zivo_allowed(event):
            return self._zivo_denied(event)
        return super().registration_confirm(event, **post)
