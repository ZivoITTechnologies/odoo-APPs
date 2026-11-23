# -*- coding: utf-8 -*-
from datetime import timedelta
from urllib.parse import urlencode

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal


class ZivoEquipmentPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'zivo_booking_count' in counters:
            values['zivo_booking_count'] = request.env['zivo.equipment.booking'].search_count(
                [('user_id', '=', request.env.user.id)])
        return values

    # ------------------------------------------------------------------
    # Catalogue & booking screen
    # ------------------------------------------------------------------
    def _zivo_equipment_domain(self):
        return [('portal_bookable', '=', True), ('active', '=', True),
                ('company_id', 'in', [request.env.company.id, False])]

    @http.route(['/equipment'], type='http', auth='user', website=True)
    def zivo_equipment_list(self, category=None, search=None, **kw):
        Equipment = request.env['zivo.equipment'].sudo()
        domain = self._zivo_equipment_domain()
        if category and str(category).isdigit():
            domain.append(('category_id', '=', int(category)))
        if search:
            domain += ['|', ('name', 'ilike', search), ('code', 'ilike', search)]
        equipments = Equipment.search(domain)
        categories = request.env['zivo.equipment.category'].sudo().search([])
        return request.render('zivoit_event_connect.portal_equipment_list', {
            'equipments': equipments,
            'categories': categories,
            'category': int(category) if category and str(category).isdigit() else False,
            'search': search or '',
            'page_name': 'zivo_equipment',
        })

    @http.route(['/equipment/<int:equipment_id>'], type='http', auth='user', website=True)
    def zivo_equipment_book_screen(self, equipment_id, date=None, error=None, **kw):
        equipment = request.env['zivo.equipment'].sudo().search(
            self._zivo_equipment_domain() + [('id', '=', equipment_id)], limit=1)
        if not equipment:
            raise request.not_found()
        today_local = equipment._zivo_utc_to_local(fields.Datetime.now()).date()
        try:
            day = fields.Date.from_string(date) if date else today_local
        except ValueError:
            day = today_local
        # skip to next open day when no date chosen
        if not date:
            for _i in range(14):
                if equipment._zivo_is_open_day(day):
                    break
                day += timedelta(days=1)
        slots = equipment._zivo_get_slots(day)
        events = request.env['event.event'].sudo().search([
            ('date_end', '>=', fields.Datetime.now()), ('stage_id.pipe_end', '=', False)], limit=50, order='date_begin')
        alternatives = []
        if equipment.category_id and any(not sl['bookable'] for sl in slots) or not slots:
            for other in request.env['zivo.equipment'].sudo().search(self._zivo_equipment_domain() + [
                    ('category_id', '=', equipment.category_id.id), ('id', '!=', equipment.id)], limit=4):
                free = [sl for sl in other._zivo_get_slots(day) if sl['bookable']]
                if free:
                    alternatives.append({'equipment': other, 'free_slots': len(free), 'first': free[0]['label']})
        return request.render('zivoit_event_connect.portal_equipment_book', {
            'alternatives': alternatives,
            'equipment': equipment,
            'day': day,
            'prev_day': day - timedelta(days=1),
            'next_day': day + timedelta(days=1),
            'min_day': today_local,
            'slots': slots,
            'events': events,
            'error': error,
            'page_name': 'zivo_equipment',
        })

    @http.route(['/equipment/<int:equipment_id>/book'], type='http', auth='user', website=True, methods=['POST'])
    def zivo_equipment_book_submit(self, equipment_id, **post):
        equipment = request.env['zivo.equipment'].sudo().search(
            self._zivo_equipment_domain() + [('id', '=', equipment_id)], limit=1)
        if not equipment:
            raise request.not_found()
        day = post.get('date')
        selected = request.httprequest.form.getlist('slot')

        def redirect_err(msg):
            return request.redirect('/equipment/%s?%s' % (equipment.id, urlencode({'date': day or '', 'error': msg})))
        if not selected:
            return redirect_err('Please select at least one time slot.')
        try:
            starts = sorted(fields.Datetime.from_string(s) for s in selected)
        except ValueError:
            return redirect_err('Invalid slot selection.')
        step = timedelta(hours=equipment.slot_duration)
        for prev, cur in zip(starts, starts[1:]):
            if cur - prev != step:
                return redirect_err('Please select consecutive time slots.')
        try:
            quantity = max(int(post.get('quantity') or 1), 1)
        except ValueError:
            quantity = 1
        event_id = int(post['event_id']) if (post.get('event_id') or '').isdigit() else False
        vals = {
            'equipment_id': equipment.id,
            'quantity': quantity,
            'start': starts[0],
            'stop': starts[-1] + step,
            'user_id': request.env.user.id,
            'event_id': event_id,
            'purpose': (post.get('purpose') or '').strip()[:2000] or False,
            'source': 'portal',
            'company_id': equipment.company_id.id or request.env.company.id,
        }
        # validate the requested slots against the slot engine (min notice, horizon, availability)
        slots = {s['start']: s for s in equipment._zivo_get_slots(equipment._zivo_utc_to_local(starts[0]).date())}
        for start in starts:
            slot = slots.get(start)
            if not slot or not slot['bookable'] or slot['available'] < quantity:
                return redirect_err('One of the selected slots is no longer available.')
        try:
            with request.env.cr.savepoint():
                booking = request.env['zivo.equipment.booking'].sudo().create(vals)
                booking.action_submit()
        except (ValidationError, UserError, AccessError) as exc:
            return redirect_err(exc.args[0] if exc.args else 'Booking failed.')
        return request.redirect('/my/equipment-bookings/%s?message=submitted' % booking.id)

    # ------------------------------------------------------------------
    # My bookings
    # ------------------------------------------------------------------
    @http.route(['/my/equipment-bookings'], type='http', auth='user', website=True)
    def zivo_my_bookings(self, filterby='all', **kw):
        domain = [('user_id', '=', request.env.user.id)]
        filters = {
            'all': ('All', []),
            'pending': ('Waiting Approval', [('state', '=', 'submitted')]),
            'approved': ('Approved', [('state', '=', 'approved')]),
            'closed': ('Closed', [('state', 'in', ('rejected', 'cancel', 'done'))]),
        }
        domain += filters.get(filterby, filters['all'])[1]
        bookings = request.env['zivo.equipment.booking'].sudo().search(domain, order='start desc', limit=200)
        values = self._prepare_portal_layout_values()
        values.update({
            'bookings': bookings,
            'filterby': filterby,
            'filters': filters,
            'page_name': 'zivo_bookings',
        })
        return request.render('zivoit_event_connect.portal_my_bookings', values)

    def _zivo_my_booking(self, booking_id):
        booking = request.env['zivo.equipment.booking'].sudo().browse(booking_id).exists()
        if not booking or booking.user_id != request.env.user:
            return None
        return booking

    @http.route(['/my/equipment-bookings/<int:booking_id>'], type='http', auth='user', website=True)
    def zivo_my_booking(self, booking_id, message=None, **kw):
        booking = self._zivo_my_booking(booking_id)
        if not booking:
            raise request.not_found()
        values = self._prepare_portal_layout_values()
        values.update({'booking': booking, 'message': message, 'page_name': 'zivo_bookings'})
        return request.render('zivoit_event_connect.portal_my_booking', values)

    @http.route(['/my/equipment-bookings/<int:booking_id>/cancel'], type='http', auth='user', website=True,
                methods=['POST'])
    def zivo_my_booking_cancel(self, booking_id, **kw):
        booking = self._zivo_my_booking(booking_id)
        if not booking:
            raise request.not_found()
        if booking.state in ('draft', 'submitted', 'approved'):
            booking.action_cancel()
        return request.redirect('/my/equipment-bookings/%s?message=cancelled' % booking.id)
