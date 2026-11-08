# -*- coding: utf-8 -*-
"""End-to-end business flows on a real Odoo 19 database (sale -> delivery -> courier -> tracking -> NDR/RTO)."""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from ..providers.base import ProviderError, utcnow
from ..providers.demo import DemoProvider
from .common import LogisticsCommon

DEMO_UTCNOW = 'odoo.addons.zivoit_logistics_hub.providers.demo.utcnow'


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestShippingFlow(LogisticsCommon):

    def test_01_rate_on_order(self):
        order = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 2, 'price_unit': 1499.0})]})
        res = self.carrier.rate_shipment(order)
        self.assertTrue(res['success'], res['error_message'])
        self.assertGreater(res['price'], 0)
        self.assertIn('Recommended', res['warning_message'])
        self.assertIn('AI score', res['warning_message'])
        # non serviceable destination: clear error, no crash
        order.partner_shipping_id = self.bad_customer
        res = self.carrier.rate_shipment(order)
        self.assertFalse(res['success'])
        self.assertIn('No courier', res['error_message'])

    def test_02_auto_ship_on_validation_and_tracking(self):
        order = self._create_order()
        picking = order.picking_ids
        self.assertEqual(picking.carrier_id, self.carrier)
        self._validate(picking)
        shipment = picking.zivo_shipment_id
        self.assertTrue(shipment, 'courier must be booked automatically on validation')
        self.assertEqual(picking.carrier_tracking_ref, shipment.tracking_number)
        self.assertEqual(shipment.state, 'booked')
        self.assertEqual(shipment.sale_id, order)
        self.assertEqual(shipment.dest_zip, '560038')
        self.assertEqual(shipment.zone, 'metro')
        self.assertTrue(shipment.label_attachment_id.raw.startswith(b'%PDF'))
        self.assertTrue(shipment.ai_edd and shipment.ai_edd_basis)
        self.assertTrue(shipment.ai_recommendation)
        self.assertEqual(shipment.rto_risk_level, 'low')
        self.assertGreater(picking.carrier_price, 0)
        self.assertIn('/logistics/track/', self.carrier.get_tracking_link(picking))
        self.assertTrue(any(m.attachment_ids for m in picking.message_ids), 'label attached on the transfer')
        # tracking: sandbox with step 0 jumps to the end of the forward flow
        picking.action_zivo_track()
        self.assertEqual(shipment.state, 'delivered')
        self.assertTrue(shipment.delivered_at and shipment.is_final)
        self.assertEqual(set(shipment.event_ids.mapped('normalized_status')) >= {'picked_up', 'in_transit', 'out_for_delivery', 'delivered'}, True)
        events = len(shipment.event_ids)
        shipment.action_track()  # idempotent
        self.assertEqual(len(shipment.event_ids), events)
        self.assertEqual(shipment.ndr_state, 'none')
        # delivered shipments cannot be cancelled
        with self.assertRaises(UserError):
            shipment.action_cancel_shipment()

    def test_03_ndr_assistant_then_rto(self):
        self.acc_api.extra_config = '{"step_minutes": 60}'
        self.acc_card.extra_config = '{"step_minutes": 60}'
        order = self._create_order(partner=self.ndr_customer, cod=True)
        picking = order.picking_ids
        booked_at = utcnow()
        with patch(DEMO_UTCNOW, return_value=booked_at + timedelta(seconds=5)):
            self._validate(picking)
        shipment = picking.zivo_shipment_id
        self.assertEqual(shipment.payment_mode, 'cod')
        self.assertAlmostEqual(shipment.cod_amount, picking._zivo_picking_value())
        with patch(DEMO_UTCNOW, return_value=booked_at + timedelta(minutes=60 * 5 + 30)):
            shipment.action_track()
        self.assertEqual(shipment.state, 'ndr')
        self.assertEqual(shipment.ndr_state, 'open')
        self.assertEqual(shipment.ndr_count, 1)
        self.assertEqual(shipment.ndr_category, 'customer_unavailable')
        self.assertEqual(shipment.ndr_suggested_action, 'reattempt')
        self.assertIn(order.name, shipment.ndr_customer_message)
        self.assertTrue(shipment.activity_ids, 'an NDR follow-up activity is scheduled')
        action = shipment.action_ndr()
        wizard = self.env['zivo.logistics.ndr.action'].with_context(action['context']).create({})
        self.assertEqual(wizard.action, 'reattempt')
        wizard.action_confirm()
        self.assertEqual(shipment.ndr_state, 'actioned')
        self.assertFalse(shipment.activity_ids)
        with patch(DEMO_UTCNOW, return_value=booked_at + timedelta(minutes=60 * 9 + 30)):
            shipment.action_track()
        self.assertEqual(shipment.state, 'rto_delivered')
        self.assertEqual(shipment.ndr_state, 'closed')
        # the customer's next COD order is riskier
        risk = self.env['zivo.logistics.ai']._risk_values(self.ndr_customer, 1499.0, True)
        self.assertIn('previous RTO', risk['rto_risk_reasons'])

    def test_04_missed_ndr_is_recorded(self):
        order = self._create_order(partner=self.ndr_customer, cod=True)
        self._validate(order.picking_ids)
        shipment = order.picking_ids.zivo_shipment_id
        shipment.action_track()  # step 0: jumps straight to RTO delivered
        self.assertEqual(shipment.state, 'rto_delivered')
        self.assertEqual(shipment.ndr_count, 1)
        self.assertEqual(shipment.ndr_state, 'closed')

    def test_05_rate_shop_wizard_manual_choice(self):
        order = self._create_order()
        picking = order.picking_ids
        action = picking.action_zivo_rate_shop()
        wizard = self.env['zivo.logistics.rate.shop'].browse(action['res_id'])
        self.assertGreaterEqual(len(wizard.line_ids), 3)
        self.assertEqual(wizard.line_ids.filtered('recommended').rank, 1)
        self.assertTrue(wizard.weight_text)
        card_line = wizard.line_ids.filtered(lambda l: l.account_id == self.acc_card)
        self.assertEqual(card_line.rate_source, 'rate_card')
        card_line.action_select()
        self.assertEqual(picking.zivo_selected_account_id, self.acc_card)
        self._validate(picking)
        self.assertEqual(picking.zivo_shipment_id.account_id, self.acc_card)
        self.assertEqual(picking.zivo_shipment_id.service_code, 'SURF')
        wizard.strategy = 'fastest'
        wizard.action_compute()
        self.assertTrue(wizard.line_ids)

    def test_06_rate_card_pricing(self):
        order = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 2, 'price_unit': 1000.0})]})
        order.zivo_payment_mode = 'cod'
        request = self.carrier._zivo_request_from_order(order)
        quote = self.acc_card._rate_card_quote(request)[0]
        # 1.6 kg -> base 0.5 kg + 3 slabs; COD max(25, 1.5% of 2300) ; +18% GST
        expected = round((20.0 + 3 * 18.0 + max(25.0, request.cod_amount * 0.015)) * 1.18, 2)
        self.assertAlmostEqual(quote.price, expected)
        self.assertEqual(quote.extra['zone'], 'metro')

    def test_07_smart_failover(self):
        original = DemoProvider.create_shipment

        def flaky(provider_self, request):
            if provider_self.config.extra.get('fail'):
                raise ProviderError('Courier API down')
            return original(provider_self, request)
        order = self._create_order()
        picking = order.picking_ids
        request = self.carrier._zivo_request_from_picking(picking)
        ranked, _errors = self.carrier._zivo_ranked_options(request, self.carrier._zivo_ctx(self.customer, self.env.company))
        first, second = ranked[0]['account'], next(o['account'] for o in ranked if o['account'] != ranked[0]['account'])
        first.extra_config = '{"step_minutes": 0, "fail": 1}'
        with patch.object(DemoProvider, 'create_shipment', autospec=True, side_effect=flaky):
            self._validate(picking)
        shipment = picking.zivo_shipment_id
        self.assertTrue(shipment)
        self.assertEqual(shipment.account_id, second)
        self.assertTrue(any('failover' in (m.body or '').lower() for m in shipment.message_ids))

    def test_08_hold_high_risk_cod_and_force(self):
        for _i in range(4):
            self.env['zivo.logistics.shipment'].create({
                'account_id': self.acc_api.id, 'state': 'rto_delivered', 'partner_id': self.customer.id,
                'tracking_number': 'OLD%s' % _i, 'dest_zip': '560038'})
        self.carrier.write({'zivo_hold_high_risk': True, 'zivo_risk_threshold': 60})
        order = self._create_order(cod=True)
        self.assertEqual(order.zivo_rto_risk_level, 'high', 'risk scored at confirmation')
        picking = order.picking_ids
        with self.assertRaises(UserError) as ctx:
            self._validate(picking)
        self.assertIn('on hold', str(ctx.exception))
        picking.with_context(zivo_force_ship=True).button_validate()
        self.assertTrue(picking.zivo_shipment_id)
        self.assertEqual(picking.zivo_shipment_id.rto_risk_level, 'high')

    def test_09_bad_address_block(self):
        self.carrier.zivo_min_address_score = 90
        weak = self.customer.copy({'street': 'Main road', 'street2': False, 'phone': '9876501234'})
        order = self._create_order(partner=weak)
        with self.assertRaises(UserError) as ctx:
            self._validate(order.picking_ids)
        self.assertIn('Address quality', str(ctx.exception))

    def test_10_cancel(self):
        order = self._create_order()
        self.acc_api.extra_config = '{"step_minutes": 600}'
        self.acc_card.extra_config = '{"step_minutes": 600}'
        picking = order.picking_ids
        self._validate(picking)
        shipment = picking.zivo_shipment_id
        picking.cancel_shipment()
        self.assertEqual(shipment.state, 'cancelled')
        self.assertFalse(picking.carrier_tracking_ref)

    def test_11_allocation_rules(self):
        self.env['zivo.logistics.allocation.rule'].create({
            'name': 'Prepaid metro: rate card only', 'payment_mode': 'prepaid', 'zone': 'metro', 'strategy': 'cheapest',
            'account_ids': [(6, 0, self.acc_card.ids)]})
        self.carrier.zivo_strategy = 'rules'
        order = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 1, 'price_unit': 1499.0})]})
        request = self.carrier._zivo_request_from_order(order)
        ranked, _errors = self.carrier._zivo_ranked_options(request, self.carrier._zivo_ctx(self.customer, self.env.company))
        self.assertEqual({o['account'] for o in ranked}, {self.acc_card})
        order.zivo_payment_mode = 'cod'  # rule does not match COD -> all accounts compete
        request = self.carrier._zivo_request_from_order(order)
        ranked, _errors = self.carrier._zivo_ranked_options(request, self.carrier._zivo_ctx(self.customer, self.env.company))
        self.assertEqual({o['account'] for o in ranked}, {self.acc_card, self.acc_api})

    def test_12_fixed_account_and_limits(self):
        fixed = self.carrier.copy({'name': 'Sandbox fixed', 'zivo_strategy': 'fixed', 'zivo_account_id': self.acc_api.id,
                                   'zivo_service_code': 'EXP'})
        order = self._create_order(carrier=fixed)
        self._validate(order.picking_ids)
        shipment = order.picking_ids.zivo_shipment_id
        self.assertEqual((shipment.account_id, shipment.service_code), (self.acc_api, 'EXP'))
        self.acc_api.max_weight_kg = 0.5
        order2 = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 3, 'price_unit': 1499.0})]})
        res = fixed.rate_shipment(order2)
        self.assertFalse(res['success'])
        self.assertIn('exceeds', res['error_message'])

    def test_13_job_queue_retry_and_dead_letter(self):
        order = self._create_order()
        self._validate(order.picking_ids)
        shipment = order.picking_ids.zivo_shipment_id
        Job = self.env['zivo.logistics.queue.job']
        ok = Job.enqueue(shipment, 'action_track', 'track')
        ok.run()
        self.assertEqual(ok.state, 'done')
        with patch.object(type(shipment), 'action_track', side_effect=ProviderError('timeout', retryable=True)):
            retry = Job.enqueue(shipment, 'action_track', 'track')
            retry.run()
            self.assertEqual((retry.state, retry.attempts), ('failed', 1))
            self.assertGreater(retry.next_run_at, fields.Datetime.now())
            retry.max_attempts = 2
            retry.run()
            self.assertEqual(retry.state, 'dead')
        with patch.object(type(shipment), 'action_track', side_effect=ProviderError('bad request', retryable=False)):
            dead = Job.enqueue(shipment, 'action_track', 'track')
            dead.run()
            self.assertEqual(dead.state, 'dead')
        dead.action_retry()
        self.assertEqual(dead.state, 'pending')
        Job._cron_process()
        self.assertEqual(dead.state, 'done')

    def test_14_cron_tracking_and_kpis(self):
        order = self._create_order()
        self._validate(order.picking_ids)
        shipment = order.picking_ids.zivo_shipment_id
        count = self.env['zivo.logistics.shipment']._cron_track()
        self.assertGreaterEqual(count, 1)
        self.assertEqual(shipment.state, 'delivered')
        account = shipment.account_id
        account.invalidate_recordset()
        self.assertEqual(account.shipment_count, 1)
        self.assertEqual(account.delivered_rate, 100.0)
        self.assertEqual(self.env['zivo.logistics.shipment']._cron_track(), 0, 'final shipments are not tracked again')

    def test_15_bulk_ship_and_merged_labels(self):
        self.carrier.integration_level = 'rate'
        pickings = self.env['stock.picking']
        for _i in range(2):
            order = self._create_order()
            pickings |= self._validate(order.picking_ids)
        self.assertFalse(any(pickings.mapped('carrier_tracking_ref')))
        self.carrier.integration_level = 'rate_and_ship'
        wizard = self.env['zivo.logistics.bulk.ship'].with_context(active_model='stock.picking', active_ids=pickings.ids).create({})
        wizard.action_ship()
        self.assertEqual(wizard.state, 'done')
        self.assertTrue(all(pickings.mapped('carrier_tracking_ref')))
        action = wizard.action_print_labels()
        att_id = int(action['url'].split('/web/content/')[1].split('?')[0])
        merged = self.env['ir.attachment'].browse(att_id)
        self.assertTrue(merged.raw.startswith(b'%PDF'))

    def test_16_account_actions(self):
        self.acc_api.action_test_connection()
        self.assertEqual(self.acc_api.state, 'connected')
        dl = self.env['zivo.logistics.account'].create({'name': 'Delhivery no key', 'provider': 'delhivery'})
        dl.action_test_connection()
        self.assertEqual(dl.state, 'error')
        self.assertIn('missing credentials', dl.last_test_message)
        self.assertTrue(dl.tracking_url_template)
        self.assertIn('/zivo_logistics/webhook/%s/' % dl.id, dl.webhook_url)
        settings = self.env['res.config.settings'].create({'zivo_ai_provider': 'local', 'zivo_track_interval_hours': 3})
        settings.execute()
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param('zivoit_logistics_hub.track_interval_hours'), '3.0')

    def test_17_return_reverse_pickup(self):
        order = self._create_order()
        picking = self._validate(order.picking_ids)
        return_wizard = self.env['stock.return.picking'].with_context(active_id=picking.id, active_model='stock.picking').create({})
        for line in return_wizard.product_return_moves:
            line.quantity = line.move_id.quantity
        res = return_wizard.action_create_returns()
        return_picking = self.env['stock.picking'].browse(res['res_id'])
        self.assertTrue(self.carrier.can_generate_return)
        self.carrier.get_return_label(return_picking)
        reverse = return_picking.zivo_shipment_ids
        self.assertTrue(reverse.is_return)
        self.assertEqual(reverse.origin_zip, '560038')
        self.assertEqual(reverse.payment_mode, 'prepaid')


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestPublicEndpoints(HttpCase):

    def setUp(self):
        super().setUp()
        self.account = self.env['zivo.logistics.account'].create({'name': 'Hook Sandbox', 'provider': 'demo'})
        self.shipment = self.env['zivo.logistics.shipment'].create({
            'account_id': self.account.id, 'tracking_number': 'ZD1234567890ABCD38', 'state': 'booked',
            'dest_phone': '9848022338', 'dest_city': 'Bengaluru', 'dest_zip': '560038', 'booked_at': fields.Datetime.now()})

    def _post(self, token, payload):
        return self.url_open('/zivo_logistics/webhook/%s/%s' % (self.account.id, token), data=json.dumps(payload),
                             headers={'Content-Type': 'application/json'})

    def test_webhook_updates_shipment(self):
        resp = self._post('wrong-token', {'awb': self.shipment.tracking_number, 'status': 'OFD'})
        self.assertEqual(resp.status_code, 403)
        resp = self._post(self.account.webhook_token, {'awb': self.shipment.tracking_number, 'status': 'OFD',
                                                       'description': 'Out for delivery', 'location': 'BLR'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['updated'], 1)
        self.shipment.invalidate_recordset()
        self.assertEqual(self.shipment.state, 'out_for_delivery')
        self.assertEqual(self.shipment.event_ids[:1].source, 'webhook')
        resp = self._post(self.account.webhook_token, {'awb': 'UNKNOWN', 'status': 'DLV'})
        self.assertEqual(resp.json()['unknown'], ['UNKNOWN'])

    def test_public_tracking_page(self):
        resp = self.url_open('/logistics/track/%s' % self.shipment.access_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.shipment.tracking_number, resp.text)
        self.assertIn('Booked', resp.text)
        resp = self.url_open('/logistics/track/%s' % ('x' * 32))
        self.assertEqual(resp.status_code, 404)
        resp = self.url_open('/logistics/track', data={'awb': self.shipment.tracking_number, 'phone': '1111'})
        self.assertIn('No shipment matches', resp.text)
        resp = self.url_open('/logistics/track', data={'awb': self.shipment.tracking_number, 'phone': '2338'})
        self.assertIn(self.shipment.tracking_number, resp.text)
