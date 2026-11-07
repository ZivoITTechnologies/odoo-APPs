# -*- coding: utf-8 -*-
import base64
from datetime import datetime, time, timedelta
from urllib.parse import quote as url_quote

import pytz

from odoo import _, fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager

PAGE_SIZE = 20
ACTIVE_TRIP_STATES = ('draft', 'assigned', 'reported', 'loading', 'in_transit', 'arrived', 'delivered')


class CargoPilotPortal(CustomerPortal):
    """Transporter-facing portal: auctions, RFQs, trips, ePOD and freight bills.

    Every record is read with sudo() and filtered on the logged-in user's
    commercial partner, so portal users need no access rights of their own.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cp_transporter(self):
        partner = request.env.user.partner_id.commercial_partner_id
        return partner if partner.cp_is_transporter else request.env['res.partner']

    def _cp_auction_domain(self, transporter):
        return [('state', 'in', ('scheduled', 'live', 'closed', 'awarded')),
                '|', ('transporter_ids', 'in', transporter.ids), ('open_to_all', '=', True)]

    def _cp_get(self, model, record_id, domain):
        record = request.env[model].sudo().search([('id', '=', int(record_id))] + domain, limit=1)
        if not record:
            raise request.not_found()
        return record

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        transporter = self._cp_transporter()
        env = request.env
        if 'cp_auction_count' in counters:
            values['cp_auction_count'] = env['cp.auction'].sudo().search_count(
                [('state', 'in', ('scheduled', 'live')), '|', ('transporter_ids', 'in', transporter.ids),
                 ('open_to_all', '=', True)]) if transporter else 0
        if 'cp_trip_count' in counters:
            values['cp_trip_count'] = env['cp.trip'].sudo().search_count(
                [('transporter_id', '=', transporter.id), ('state', 'in', ACTIVE_TRIP_STATES)]) if transporter else 0
        if 'cp_rfq_count' in counters:
            values['cp_rfq_count'] = env['cp.rfq'].sudo().search_count(
                [('transporter_ids', 'in', transporter.ids), ('state', '=', 'published')]) if transporter else 0
        if 'cp_bill_count' in counters:
            values['cp_bill_count'] = env['cp.freight.bill'].sudo().search_count(
                [('transporter_id', '=', transporter.id)]) if transporter else 0
        if 'cp_shipment_count' in counters:
            customer = self._cp_customer()
            values['cp_shipment_count'] = env['cp.indent'].sudo().search_count(
                [('customer_id', '=', customer.id)]) if customer else 0
        return values

    # ------------------------------------------------------------------
    # Customers: request and follow shipments (self-service booking)
    # ------------------------------------------------------------------
    def _cp_customer(self):
        partner = request.env.user.partner_id.commercial_partner_id
        return request.env['res.partner'] if partner.cp_is_transporter else partner

    @http.route('/my/cp/shipments', type='http', auth='user', website=True)
    def cp_customer_shipments(self, message=None, **kw):
        customer = self._cp_customer()
        if not customer:
            return request.redirect('/my')
        indents = request.env['cp.indent'].sudo().search([('customer_id', '=', customer.id)],
                                                         order='required_date desc', limit=100)
        return request.render('cargo_pilot_tms.portal_my_cp_shipments', {
            'indents': indents, 'page_name': 'cp_shipments', 'message': message,
        })

    @http.route('/my/cp/shipments/new', type='http', auth='user', website=True, methods=['GET', 'POST'])
    def cp_customer_book(self, **post):
        customer = self._cp_customer()
        if not customer:
            return request.redirect('/my')
        env = request.env
        locations = env['cp.location'].sudo().search([], order='name')
        vehicle_types = env['cp.vehicle.type'].sudo().search([], order='capacity_ton')
        error = None
        if request.httprequest.method == 'POST':
            try:
                origin = locations.filtered(lambda l: l.id == int(post.get('origin_id') or 0))
                dest = locations.filtered(lambda l: l.id == int(post.get('destination_id') or 0))
                vtype = vehicle_types.filtered(lambda v: v.id == int(post.get('vehicle_type_id') or 0))
                if not (origin and dest and vtype) or origin == dest:
                    raise UserError(_('Choose a pickup point, a different delivery point and a truck type.'))
                pickup = datetime.strptime(post.get('pickup'), '%Y-%m-%dT%H:%M')
                tz = pytz.timezone(env.user.tz or customer.tz or 'UTC')
                pickup_utc = tz.localize(pickup).astimezone(pytz.utc).replace(tzinfo=None)
                if pickup_utc < datetime.utcnow():
                    raise UserError(_('Pick a time in the future.'))
                lane = env['cp.lane'].sudo().search([('origin_id', '=', origin.id),
                                                     ('destination_id', '=', dest.id)], limit=1) \
                    or env['cp.lane'].sudo().create({'origin_id': origin.id, 'destination_id': dest.id})
                indent = env['cp.indent'].sudo().create({
                    'lane_id': lane.id,
                    'vehicle_type_id': vtype.id,
                    'vehicle_count': max(int(post.get('vehicle_count') or 1), 1),
                    'required_date': pickup_utc,
                    'customer_id': customer.id,
                    'consignee_id': customer.id,
                    'origin': (post.get('reference') or '')[:64],
                    'source_channel': 'portal',
                    'notes': post.get('notes'),
                    'line_ids': [(0, 0, {'description': (post.get('material') or _('General cargo'))[:120],
                                         'quantity': 1, 'uom_label': _('Lot'),
                                         'weight_ton': float(post.get('weight_ton') or 0)})],
                })
                indent.message_post(body=_('Requested by %s from the customer portal.', customer.name))
                return request.redirect('/my/cp/shipments?message=%s' % url_quote(
                    _('Request %s received. We will confirm the truck shortly.', indent.name)))
            except (UserError, ValueError, TypeError) as exc:
                error = str(exc.args[0]) if getattr(exc, 'args', None) else str(exc)
        return request.render('cargo_pilot_tms.portal_my_cp_book', {
            'locations': locations, 'vehicle_types': vehicle_types, 'page_name': 'cp_book',
            'error': error, 'post': post,
        })

    # ------------------------------------------------------------------
    # Transporters: self-book a dock appointment
    # ------------------------------------------------------------------
    def _cp_free_slots(self, trip, days=3, open_hour=6, close_hour=20):
        docks = trip.origin_id.dock_ids.filtered(lambda d: d.dock_type in ('loading', 'both'))
        if not docks:
            return []
        tz = pytz.timezone(request.env.user.tz or trip.company_id.partner_id.tz or 'UTC')
        now_utc = datetime.utcnow()
        local_today = pytz.utc.localize(now_utc).astimezone(tz).date()
        Gate = request.env['cp.gate.entry'].sudo()
        horizon_end = now_utc + timedelta(days=days + 1)
        booked = Gate.search([('dock_id', 'in', docks.ids), ('state', '!=', 'cancel'),
                              ('slot_start', '<', horizon_end), ('slot_end', '>', now_utc)])
        slots = []
        for offset in range(days):
            day = local_today + timedelta(days=offset)
            for hour in range(open_hour, close_hour):
                local_start = tz.localize(datetime.combine(day, time(hour)))
                start = local_start.astimezone(pytz.utc).replace(tzinfo=None)
                end = start + timedelta(hours=1)
                if start <= now_utc + timedelta(minutes=30):
                    continue
                for dock in docks:
                    clash = booked.filtered(lambda g: g.dock_id == dock and g.slot_start < end and g.slot_end > start)
                    if not clash:
                        slots.append({'value': '%s|%s' % (dock.id, start.strftime('%Y-%m-%d %H:%M:%S')),
                                      'day': local_start.strftime('%a %d %b'),
                                      'label': '%s · %s' % (local_start.strftime('%H:%M'), dock.name)})
                        break
        return slots

    @http.route('/my/cp/trips/<int:trip_id>/appointment', type='http', auth='user', methods=['POST'], website=True)
    def cp_trip_appointment(self, trip_id, slot=None, **post):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        trip = self._cp_get('cp.trip', trip_id, [('transporter_id', '=', transporter.id)])
        url = '/my/cp/trips/%s' % trip.id
        try:
            dock_id, start = (slot or '').split('|')
            start = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
            dock = trip.origin_id.dock_ids.filtered(lambda d: d.id == int(dock_id))
            if not dock or trip.state not in ('draft', 'assigned'):
                raise UserError(_('This slot cannot be booked for this trip.'))
            Gate = request.env['cp.gate.entry'].sudo()
            Gate.search([('trip_id', '=', trip.id), ('state', '=', 'expected')]).write({'state': 'cancel'})
            Gate.create({
                'location_id': trip.origin_id.id, 'trip_id': trip.id, 'dock_id': dock.id,
                'vehicle_number': trip.vehicle_number or _('TBA'), 'driver_name': trip.driver_name,
                'slot_start': start, 'slot_end': start + timedelta(hours=1),
            })
            trip.message_post(body=_('%(t)s booked %(d)s for loading.', t=transporter.name, d=dock.name))
        except (UserError, ValueError) as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        except Exception:  # slot taken meanwhile (constraint)
            return request.redirect('%s?error=%s' % (url, url_quote(_('That slot was just taken. Pick another.'))))
        return request.redirect('%s?message=%s' % (url, url_quote(_('Dock appointment booked.'))))

    # ------------------------------------------------------------------
    # Auctions
    # ------------------------------------------------------------------
    @http.route(['/my/cp/auctions', '/my/cp/auctions/page/<int:page>'], type='http', auth='user', website=True)
    def cp_auctions(self, page=1, filterby='open', **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        Auction = request.env['cp.auction'].sudo()
        domain = self._cp_auction_domain(transporter)
        filters = {
            'open': {'label': _('Open'), 'domain': [('state', 'in', ('scheduled', 'live'))]},
            'won': {'label': _('Won'), 'domain': [('winner_ids', 'in', transporter.ids)]},
            'all': {'label': _('All'), 'domain': []},
        }
        domain += filters.get(filterby, filters['open'])['domain']
        total = Auction.search_count(domain)
        pager = portal_pager(url='/my/cp/auctions', url_args={'filterby': filterby}, total=total,
                             page=page, step=PAGE_SIZE)
        auctions = Auction.search(domain, order='date_end asc', limit=PAGE_SIZE, offset=pager['offset'])
        return request.render('cargo_pilot_tms.portal_my_cp_auctions', {
            'auctions': auctions, 'transporter': transporter, 'pager': pager, 'page_name': 'cp_auctions',
            'searchbar_filters': filters, 'filterby': filterby, 'default_url': '/my/cp/auctions',
        })

    @http.route('/my/cp/auctions/<int:auction_id>', type='http', auth='user', website=True)
    def cp_auction_detail(self, auction_id, message=None, error=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        auction = self._cp_get('cp.auction', auction_id, self._cp_auction_domain(transporter))
        my_bids = auction.bid_ids.filtered(lambda b: b.transporter_id == transporter).sorted('bid_time', reverse=True)
        return request.render('cargo_pilot_tms.portal_my_cp_auction', {
            'auction': auction, 'transporter': transporter, 'my_bids': my_bids,
            'my_latest': my_bids.filtered('is_latest')[:1], 'my_rank': auction.cp_rank_for(transporter),
            'page_name': 'cp_auction', 'message': message, 'error': error,
        })

    @http.route('/my/cp/auctions/<int:auction_id>/bid', type='http', auth='user', methods=['POST'], website=True)
    def cp_auction_bid(self, auction_id, rate=None, vehicles=None, note=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        auction = self._cp_get('cp.auction', auction_id, self._cp_auction_domain(transporter))
        url = '/my/cp/auctions/%s' % auction.id
        try:
            auction.cp_place_bid(transporter, float(rate or 0), int(vehicles or auction.vehicle_count),
                                 channel='portal', note=note)
        except (UserError, ValueError) as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(getattr(exc, 'args', [exc])[0]))))
        return request.redirect('%s?message=%s' % (url, url_quote(_('Bid placed.'))))

    # ------------------------------------------------------------------
    # RFQs
    # ------------------------------------------------------------------
    @http.route('/my/cp/rfqs', type='http', auth='user', website=True)
    def cp_rfqs(self, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        rfqs = request.env['cp.rfq'].sudo().search(
            [('transporter_ids', 'in', transporter.ids), ('state', '!=', 'draft')], order='date_deadline desc')
        return request.render('cargo_pilot_tms.portal_my_cp_rfqs', {'rfqs': rfqs, 'page_name': 'cp_rfqs'})

    @http.route('/my/cp/rfqs/<int:rfq_id>', type='http', auth='user', website=True)
    def cp_rfq_detail(self, rfq_id, message=None, error=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        rfq = self._cp_get('cp.rfq', rfq_id, [('transporter_ids', 'in', transporter.ids), ('state', '!=', 'draft')])
        quotes = {q.line_id.id: q for q in rfq.quote_ids.filtered(lambda q: q.transporter_id == transporter)}
        return request.render('cargo_pilot_tms.portal_my_cp_rfq', {
            'rfq': rfq, 'quotes': quotes, 'page_name': 'cp_rfq', 'message': message, 'error': error,
        })

    @http.route('/my/cp/rfqs/<int:rfq_id>/quote', type='http', auth='user', methods=['POST'], website=True)
    def cp_rfq_quote(self, rfq_id, **post):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        rfq = self._cp_get('cp.rfq', rfq_id, [('transporter_ids', 'in', transporter.ids)])
        url = '/my/cp/rfqs/%s' % rfq.id
        if rfq.state != 'published' or rfq.date_deadline < fields.Datetime.now():
            return request.redirect('%s?error=%s' % (url, url_quote(_('This RFQ is closed for quotes.'))))
        Quote = request.env['cp.rfq.quote'].sudo()
        saved = 0
        for line in rfq.line_ids:
            raw = (post.get('rate_%s' % line.id) or '').strip()
            if not raw:
                continue
            try:
                rate = float(raw)
            except ValueError:
                continue
            if rate <= 0:
                continue
            existing = Quote.search([('line_id', '=', line.id), ('transporter_id', '=', transporter.id)], limit=1)
            vals = {'rate': rate, 'remarks': post.get('remarks_%s' % line.id), 'submitted_on': fields.Datetime.now()}
            if existing:
                existing.write(vals)
            else:
                Quote.create(dict(vals, rfq_id=rfq.id, line_id=line.id, transporter_id=transporter.id))
            saved += 1
        rfq.message_post(body=_('%(t)s submitted %(n)s lane quote(s) from the portal.', t=transporter.name, n=saved))
        return request.redirect('%s?message=%s' % (url, url_quote(_('%s quote(s) saved.', saved))))

    # ------------------------------------------------------------------
    # Trips
    # ------------------------------------------------------------------
    @http.route(['/my/cp/trips', '/my/cp/trips/page/<int:page>'], type='http', auth='user', website=True)
    def cp_trips(self, page=1, filterby='active', **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        Trip = request.env['cp.trip'].sudo()
        filters = {
            'active': {'label': _('Active'), 'domain': [('state', 'in', ACTIVE_TRIP_STATES)]},
            'epod': {'label': _('ePOD Due'), 'domain': [('state', 'in', ('arrived', 'delivered')),
                                                        ('epod_state', '!=', 'verified')]},
            'all': {'label': _('All'), 'domain': []},
        }
        domain = [('transporter_id', '=', transporter.id)] + filters.get(filterby, filters['active'])['domain']
        total = Trip.search_count(domain)
        pager = portal_pager(url='/my/cp/trips', url_args={'filterby': filterby}, total=total,
                             page=page, step=PAGE_SIZE)
        trips = Trip.search(domain, limit=PAGE_SIZE, offset=pager['offset'])
        return request.render('cargo_pilot_tms.portal_my_cp_trips', {
            'trips': trips, 'pager': pager, 'page_name': 'cp_trips', 'searchbar_filters': filters,
            'filterby': filterby, 'default_url': '/my/cp/trips',
        })

    @http.route('/my/cp/trips/<int:trip_id>', type='http', auth='user', website=True)
    def cp_trip_detail(self, trip_id, message=None, error=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        trip = self._cp_get('cp.trip', trip_id, [('transporter_id', '=', transporter.id)])
        appointment = request.env['cp.gate.entry'].sudo().search(
            [('trip_id', '=', trip.id), ('state', '!=', 'cancel'), ('slot_start', '!=', False)], limit=1)
        slots = self._cp_free_slots(trip) if trip.state in ('draft', 'assigned') else []
        days = []
        for slot in slots:
            if not days or days[-1]['day'] != slot['day']:
                days.append({'day': slot['day'], 'slots': []})
            days[-1]['slots'].append(slot)
        return request.render('cargo_pilot_tms.portal_my_cp_trip', {
            'trip': trip, 'page_name': 'cp_trip', 'message': message, 'error': error,
            'appointment': appointment, 'slot_days': days,
            'decline_reasons': request.env['cp.trip.offer']._fields['decline_reason'].selection,
        })

    @http.route('/my/cp/trips/<int:trip_id>/update', type='http', auth='user', methods=['POST'], website=True)
    def cp_trip_update(self, trip_id, **post):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        trip = self._cp_get('cp.trip', trip_id, [('transporter_id', '=', transporter.id)])
        url = '/my/cp/trips/%s' % trip.id
        if trip.state not in ('draft', 'assigned', 'reported', 'loading'):
            return request.redirect('%s?error=%s' % (url, url_quote(_('Vehicle details are locked after dispatch.'))))
        vals = {}
        for key in ('vehicle_number', 'driver_name', 'driver_phone', 'eway_bill_no', 'docket_number',
                    'appointment_ref'):
            value = (post.get(key) or '').strip()
            if value:
                vals[key] = value.upper() if key == 'vehicle_number' else value
        if vals:
            trip.write(vals)
            trip.message_post(body=_('%s updated vehicle details from the portal.', transporter.name))
        if post.get('confirm') and trip.state == 'draft' and (trip.vehicle_number or trip.docket_number):
            try:
                trip.action_assign()
            except UserError as exc:
                return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        return request.redirect('%s?message=%s' % (url, url_quote(_('Trip updated.'))))

    @http.route('/my/cp/trips/<int:trip_id>/offer', type='http', auth='user', methods=['POST'], website=True)
    def cp_trip_offer(self, trip_id, decision=None, reason=None, note=None, **post):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        trip = self._cp_get('cp.trip', trip_id, [('transporter_id', '=', transporter.id)])
        url = '/my/cp/trips/%s' % trip.id
        offer = trip.offer_ids.filtered(lambda o: o.state == 'pending' and o.transporter_id == transporter)[:1]
        if not offer:
            return request.redirect('%s?error=%s' % (url, url_quote(_('This booking is no longer waiting for you.'))))
        try:
            if decision == 'accept':
                offer.action_accept('portal')
                msg = _('Booking accepted. Add the truck and driver below.')
            else:
                offer.action_decline(reason or 'other', note, 'portal')
                return request.redirect('/my/cp/trips?message=%s' % url_quote(_('Booking declined.')))
        except UserError as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        return request.redirect('%s?message=%s' % (url, url_quote(msg)))

    @http.route('/my/cp/trips/<int:trip_id>/epod', type='http', auth='user', methods=['POST'], website=True)
    def cp_trip_epod(self, trip_id, **post):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        trip = self._cp_get('cp.trip', trip_id, [('transporter_id', '=', transporter.id)])
        url = '/my/cp/trips/%s' % trip.id
        try:
            epod = cp_create_epod_from_post(trip, post, request.httprequest.files.getlist('photos'), 'portal')
        except (UserError, ValueError) as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        msg = _('ePOD %s verified.', epod.name) if epod.state == 'verified' else \
            _('ePOD %(n)s submitted with exceptions: %(r)s', n=epod.name, r=epod.exception_reason or '')
        return request.redirect('%s?message=%s' % (url, url_quote(msg)))

    # ------------------------------------------------------------------
    # Freight bills
    # ------------------------------------------------------------------
    @http.route('/my/cp/bills', type='http', auth='user', website=True)
    def cp_bills(self, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        bills = request.env['cp.freight.bill'].sudo().search([('transporter_id', '=', transporter.id)])
        return request.render('cargo_pilot_tms.portal_my_cp_bills', {'bills': bills, 'page_name': 'cp_bills'})

    @http.route('/my/cp/bills/<int:bill_id>', type='http', auth='user', website=True)
    def cp_bill_detail(self, bill_id, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        bill = self._cp_get('cp.freight.bill', bill_id, [('transporter_id', '=', transporter.id)])
        return request.render('cargo_pilot_tms.portal_my_cp_bill', {'bill': bill, 'page_name': 'cp_bill'})


def cp_create_epod_from_post(trip, post, files, channel):
    """Shared by the transporter portal and the driver page."""
    if trip.state not in ('in_transit', 'arrived', 'delivered'):
        raise UserError(_('An ePOD can be recorded once the trip is in transit.'))
    if trip.epod_state == 'verified':
        raise UserError(_('This trip already has a verified ePOD.'))

    def _num(key, cast=float):
        raw = (post.get(key) or '').strip()
        return cast(raw) if raw else cast(0)

    attachments = request.env['ir.attachment'].sudo()
    for upload in files or []:
        if not upload or not upload.filename:
            continue
        data = upload.read()
        if len(data) > 10 * 1024 * 1024:
            raise UserError(_('Each photo must be under 10 MB.'))
        attachments |= attachments.create({
            'name': upload.filename, 'datas': base64.b64encode(data), 'res_model': 'cp.epod',
            'mimetype': upload.mimetype,
        })
    signature = post.get('signature') or ''
    if signature.startswith('data:image'):
        signature = signature.split(',', 1)[1]
    vals = {
        'trip_id': trip.id,
        'channel': channel,
        'receiver_name': post.get('receiver_name'),
        'receiver_phone': post.get('receiver_phone'),
        'received_weight_ton': _num('received_weight_ton'),
        'received_packages': _num('received_packages', int),
        'shortage_packages': _num('shortage_packages', int),
        'damaged_packages': _num('damaged_packages', int),
        'refused_by_consignee': bool(post.get('refused_by_consignee')),
        'remarks': post.get('remarks'),
        'scanned_serials': post.get('scanned_serials') or False,
        'grn_number': (post.get('grn_number') or '').strip() or False,
        'latitude': _num('latitude'),
        'longitude': _num('longitude'),
        'signature': signature or False,
        'photo_ids': [(6, 0, attachments.ids)],
    }
    epod = request.env['cp.epod'].sudo().create(vals)
    attachments.write({'res_id': epod.id})
    epod.action_submit()
    return epod
