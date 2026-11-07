# -*- coding: utf-8 -*-
import json
from urllib.parse import quote as url_quote

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

from .portal import cp_create_epod_from_post


class CargoPilotPublic(http.Controller):
    """Token-protected pages that need no login: customer tracking and driver mobile page."""

    def _cp_trip_by(self, field, token):
        if not token or len(token) < 16:
            raise request.not_found()
        trip = request.env['cp.trip'].sudo().search([(field, '=', token)], limit=1)
        if not trip or trip.state == 'cancel':
            raise request.not_found()
        return trip

    # ------------------------------------------------------------------
    # Booking offer link (sent by e-mail / WhatsApp, no login needed)
    # ------------------------------------------------------------------
    def _cp_offer_by_token(self, token):
        if not token or len(token) < 16:
            raise request.not_found()
        offer = request.env['cp.trip.offer'].sudo().search([('access_token', '=', token)], limit=1)
        if not offer:
            raise request.not_found()
        return offer

    @http.route('/cp/offer/<string:token>', type='http', auth='public', website=True, methods=['GET', 'POST'])
    def cp_offer(self, token, **post):
        offer = self._cp_offer_by_token(token)
        message = error = None
        if request.httprequest.method == 'POST':
            try:
                if post.get('decision') == 'accept':
                    offer.action_accept('link')
                    message = _('Thank you. The booking is confirmed. Add the truck and driver on the portal or reply '
                                'with the vehicle number.')
                elif post.get('decision') == 'decline':
                    offer.action_decline(post.get('reason') or 'other', post.get('note'), 'link')
                    message = _('Noted. The load has been passed on.')
            except UserError as exc:
                error = str(exc.args[0])
        trip = offer.trip_id
        return request.render('cargo_pilot_tms.cp_offer_page', {
            'offer': offer, 'trip': trip, 'message': message, 'error': error,
            'reasons': offer._fields['decline_reason'].selection,
        })

    # ------------------------------------------------------------------
    # Customer tracking
    # ------------------------------------------------------------------
    @http.route('/cp/track/<string:token>', type='http', auth='public', website=True)
    def cp_track(self, token, **kw):
        trip = self._cp_trip_by('tracking_token', token)
        return request.render('cargo_pilot_tms.cp_public_tracking', {
            'trip': trip, 'events': trip.cp_public_timeline(), 'map_json': json.dumps(self._cp_map_data(trip)),
        })

    @staticmethod
    def _cp_map_data(trip):
        def point(loc):
            return [loc.latitude, loc.longitude] if loc and (loc.latitude or loc.longitude) else None
        pings = trip.ping_ids.sorted('ping_time')[-300:]
        current = [trip.last_latitude, trip.last_longitude] if (trip.last_latitude or trip.last_longitude) else None
        return {
            'origin': point(trip.origin_id), 'destination': point(trip.destination_id), 'current': current,
            'path': [[p.latitude, p.longitude] for p in pings],
            'origin_name': trip.origin_id.name, 'destination_name': trip.destination_id.name,
            'vehicle': trip.vehicle_number or '', 'late': trip.is_late,
        }

    @staticmethod
    def _cp_map_url(trip):
        lat, lon = trip.last_latitude, trip.last_longitude
        if not (lat or lon):
            point = trip.origin_id if trip.state in ('draft', 'assigned', 'reported', 'loading') else trip.destination_id
            lat, lon = point.latitude, point.longitude
        if not (lat or lon):
            return False
        pad = 0.08
        return ('https://www.openstreetmap.org/export/embed.html?bbox=%.5f%%2C%.5f%%2C%.5f%%2C%.5f'
                '&layer=mapnik&marker=%.5f%%2C%.5f') % (lon - pad, lat - pad, lon + pad, lat + pad, lat, lon)

    # ------------------------------------------------------------------
    # Driver page
    # ------------------------------------------------------------------
    @http.route('/cp/driver/<string:token>', type='http', auth='public', website=True)
    def cp_driver(self, token, message=None, error=None, **kw):
        trip = self._cp_trip_by('driver_token', token)
        return request.render('cargo_pilot_tms.cp_driver_page', {
            'trip': trip, 'token': token, 'message': message, 'error': error,
        })

    @http.route('/cp/driver/<string:token>/status', type='http', auth='public', methods=['POST'], website=True)
    def cp_driver_status(self, token, step=None, **post):
        trip = self._cp_trip_by('driver_token', token)
        url = '/cp/driver/%s' % token
        actions = {
            'reported': ('assigned', trip.action_report),
            'dispatch': (('assigned', 'reported', 'loading'), trip.action_dispatch),
            'arrived': ('in_transit', trip.action_mark_arrived),
        }
        if step not in actions:
            return request.redirect(url)
        allowed, method = actions[step]
        allowed = (allowed,) if isinstance(allowed, str) else allowed
        if trip.state not in allowed:
            return request.redirect('%s?error=%s' % (url, url_quote(_('That step is not available now.'))))
        try:
            method()
        except UserError as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        return request.redirect('%s?message=%s' % (url, url_quote(_('Status updated.'))))

    @http.route('/cp/driver/<string:token>/ping', type='jsonrpc', auth='public', methods=['POST'])
    def cp_driver_ping(self, token, latitude=None, longitude=None, speed=None, **kw):
        trip = self._cp_trip_by('driver_token', token)
        if trip.state not in ('assigned', 'reported', 'loading', 'in_transit', 'arrived'):
            return {'ok': False, 'state': trip.state}
        try:
            lat, lon = float(latitude), float(longitude)
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'invalid coordinates'}
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {'ok': False, 'error': 'invalid coordinates'}
        kmph = float(speed) * 3.6 if speed not in (None, '') else None  # browsers report m/s
        trip.cp_add_ping(lat, lon, speed=kmph, source='driver')
        return {'ok': True, 'state': trip.state,
                'remaining_km': round(trip.remaining_km, 1)}

    @http.route('/cp/driver/<string:token>/epod', type='http', auth='public', methods=['POST'], website=True)
    def cp_driver_epod(self, token, **post):
        trip = self._cp_trip_by('driver_token', token)
        url = '/cp/driver/%s' % token
        try:
            epod = cp_create_epod_from_post(trip, post, request.httprequest.files.getlist('photos'), 'driver')
        except (UserError, ValueError) as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        msg = _('Delivery recorded. Thank you!') if epod.state == 'verified' else \
            _('Delivery recorded; the office will review: %s', epod.exception_reason or '')
        return request.redirect('%s?message=%s' % (url, url_quote(msg)))

    # ------------------------------------------------------------------
    # Installable driver app (add to home screen)
    # ------------------------------------------------------------------
    @http.route('/cp/driver/<string:token>/manifest.webmanifest', type='http', auth='public')
    def cp_driver_manifest(self, token, **kw):
        trip = self._cp_trip_by('driver_token', token)
        manifest = {
            'name': 'Cargo Pilot Driver',
            'short_name': 'Driver',
            'description': 'Trip status, location sharing and delivery proof',
            'start_url': '/cp/driver/%s' % token,
            'scope': '/cp/driver/%s' % token,
            'display': 'standalone',
            'orientation': 'portrait',
            'background_color': '#f3f5f6',
            'theme_color': '#1f5a4d',
            'icons': [
                {'src': '/cargo_pilot_tms/static/description/icon.png', 'sizes': '256x256', 'type': 'image/png',
                 'purpose': 'any maskable'},
            ],
        }
        return request.make_response(json.dumps(manifest), headers=[
            ('Content-Type', 'application/manifest+json'), ('Cache-Control', 'no-store')])
