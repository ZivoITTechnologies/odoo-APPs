# -*- coding: utf-8 -*-
"""End-to-end tests of the v2 features: cost-effective shipping, insurance, box suggestion, rate calculator,
manifests, customer returns (portal + RMA + reverse pickup), freight / COD reconciliation, scan to ship,
proactive delay alerts."""
import base64
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import HttpCase, tagged

from ..ai import engine
from ..providers.base import NotSupported
from ..providers.demo import DemoProvider
from .common import LogisticsCommon


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestCostEngine(LogisticsCommon):

    def _opts(self):
        return [{'key': (1, 'A'), 'account_id': 1, 'price': 50.0, 'tat_days': 6, 'perf': None},
                {'key': (2, 'B'), 'account_id': 2, 'price': 80.0, 'tat_days': 3, 'perf': None},
                {'key': (3, 'C'), 'account_id': 3, 'price': 140.0, 'tat_days': 1, 'perf': None}]

    def test_engine_on_time_preference_savings(self):
        ranked = engine.score_rates(self._opts(), 'cheapest')
        self.assertEqual(ranked[0]['key'], (1, 'A'))
        ranked = engine.score_rates(self._opts(), 'cheapest_on_time', deliver_within_days=3)
        self.assertEqual([o['key'][1] for o in ranked], ['B', 'C', 'A'])  # late option always last
        self.assertIn('arrives within 3 days', ranked[0]['reasons'][0])
        self.assertIn('misses the delivery date by 3 days', ranked[-1]['reasons'][0])
        # even "cheapest" never picks a late carrier when a promise exists
        self.assertEqual(engine.score_rates(self._opts(), 'cheapest', deliver_within_days=1)[0]['key'], (3, 'C'))
        # nobody on time: still returns the ranking (best effort)
        self.assertEqual(len(engine.score_rates(self._opts(), 'cheapest', deliver_within_days=0.5)), 3)
        # service preference: B is 30 more than A
        ranked = engine.score_rates(self._opts(), 'cheapest')
        self.assertEqual(engine.apply_preference(list(ranked), {2}, margin_amount=20)[0]['key'], (1, 'A'))
        promoted = engine.apply_preference(list(ranked), {2}, margin_amount=30)
        self.assertEqual(promoted[0]['key'], (2, 'B'))
        self.assertEqual(promoted[0]['rank'], 1)
        self.assertIn('preferred carrier', promoted[0]['reasons'][0])
        self.assertEqual(engine.apply_preference(list(ranked), {2}, margin_pct=60)[0]['key'], (2, 'B'))
        saving = engine.savings(ranked)
        self.assertEqual((saving['max_price'], saving['saved_vs_max'], saving['options']), (140.0, 90.0, 3))
        self.assertAlmostEqual(saving['saved_pct'], 64.3)
        self.assertEqual(engine.savings([])['options'], 0)

    def test_engine_box_and_insurance(self):
        boxes = [{'id': 1, 'name': 'S', 'length': 10, 'width': 10, 'height': 10},
                 {'id': 2, 'name': 'M', 'length': 20, 'width': 15, 'height': 10, 'max_weight': 5},
                 {'id': 3, 'name': 'L', 'length': 40, 'width': 30, 'height': 30},
                 {'id': 4, 'name': 'Flat', 'length': 0, 'width': 30, 'height': 30}]
        best = engine.suggest_box(2000, 0, 0.8, boxes)
        self.assertEqual(best['box']['name'], 'M')
        self.assertAlmostEqual(best['fill_pct'], 66.7)
        self.assertEqual(engine.suggest_box(2000, 0, 6.0, boxes)['box']['name'], 'L')  # M weight limit
        self.assertEqual(engine.suggest_box(2000, 35, 0.8, boxes)['box']['name'], 'L')  # long item
        self.assertIsNone(engine.suggest_box(50000, 0, 1, boxes))
        self.assertEqual(engine.insurance_premium(5000, 0.5, 10), 25.0)
        self.assertEqual(engine.insurance_premium(500, 0.5, 10), 10.0)
        self.assertEqual(engine.insurance_premium(0, 0.5, 10), 0.0)


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestCostEffectiveShipping(LogisticsCommon):

    def _ranked(self, carrier, order=None, **ctx_extra):
        order = order or self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 1, 'price_unit': 1499.0})]})
        request = carrier._zivo_request_from_order(order)
        ctx = carrier._zivo_ctx(order.partner_shipping_id, order.company_id, value=request.declared_value,
                                deliver_by=order.commitment_date)
        ctx.update(ctx_extra)
        return carrier._zivo_ranked_options(request, ctx)[0]

    def test_cheapest_on_time(self):
        self.carrier.zivo_strategy = 'cheapest'
        self.assertEqual(self._ranked(self.carrier)[0]['quote'].service_code, 'SURF')  # 6 days, cheapest
        self.carrier.write({'zivo_strategy': 'cheapest_on_time', 'zivo_deliver_within_days': 3})
        best = self._ranked(self.carrier)[0]
        self.assertEqual(best['quote'].service_code, 'STD')
        self.assertTrue(best['on_time'])
        self.carrier.zivo_deliver_within_days = 2
        self.assertEqual(self._ranked(self.carrier)[0]['quote'].service_code, 'EXP')

    def test_commitment_date_drives_promise(self):
        self.carrier.write({'zivo_strategy': 'cheapest', 'zivo_deliver_within_days': 0})
        order = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 1, 'price_unit': 1499.0})]})
        order.commitment_date = engine.add_business_days(fields.Datetime.now(), 2)
        days = self.carrier._zivo_deliver_within({'deliver_by': order.commitment_date})
        self.assertEqual(days, 2)
        best = self._ranked(self.carrier, order)[0]
        self.assertEqual(best['quote'].service_code, 'EXP')
        self.assertLessEqual(best['quote'].tat_days, 2)

    def test_preferred_carrier_and_exclusions(self):
        self.carrier.zivo_strategy = 'cheapest'
        ranked = self._ranked(self.carrier)
        card, std = ranked[0], next(o for o in ranked if o['quote'].service_code == 'STD')
        gap = std['price'] - card['price']  # prices are in company currency
        self.carrier.write({'zivo_preferred_account_ids': [(6, 0, self.acc_api.ids)], 'zivo_preference_margin': gap + 0.01})
        best = self._ranked(self.carrier)[0]
        self.assertEqual((best['account'], best['quote'].service_code), (self.acc_api, 'STD'))
        self.assertIn('preferred carrier', best['reasons'][0])
        self.carrier.zivo_preference_margin = gap / 2
        self.assertEqual(self._ranked(self.carrier)[0]['account'], self.acc_card)
        self.carrier.write({'zivo_preference_margin': 0, 'zivo_preference_margin_pct': 100.0 * gap / card['price'] + 1})
        self.assertEqual(self._ranked(self.carrier)[0]['account'], self.acc_api)
        self.carrier.zivo_preference_margin_pct = 0
        self.carrier.write({'zivo_preferred_account_ids': [(5,)], 'zivo_excluded_services': 'SURF, EXP'})
        ranked = self._ranked(self.carrier)
        self.assertEqual({o['quote'].service_code for o in ranked}, {'STD'})

    def test_contract_rate_card_without_api_credentials(self):
        ekart = self.env['zivo.logistics.account'].create({
            'name': 'Ekart contract', 'provider': 'ekart', 'rate_source': 'rate_card',
            'rate_card_ids': [(0, 0, {'service_code': 'SURFACE', 'service_name': 'Ekart Surface', 'zone': 'any',
                                      'base_price': 15.0, 'additional_price': 14.0, 'tat_days': 5})]})
        api_only = self.env['zivo.logistics.account'].create({'name': 'Ekart API', 'provider': 'ekart', 'rate_source': 'api'})
        self.carrier.write({'zivo_strategy': 'cheapest',
                            'zivo_account_ids': [(4, ekart.id), (4, api_only.id)]})
        order = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 1, 'price_unit': 1499.0})]})
        request = self.carrier._zivo_request_from_order(order)
        ranked, errors = self.carrier._zivo_ranked_options(
            request, self.carrier._zivo_ctx(self.customer, self.company, value=request.declared_value))
        self.assertEqual((ranked[0]['account'], ranked[0]['quote'].source), (ekart, 'rate_card'))
        self.assertTrue(any(e.startswith('Ekart API') and 'missing credentials' in e for e in errors))

    def test_booking_records_savings_insurance_promise(self):
        self.carrier.write({'zivo_insurance_mode': 'above', 'zivo_insurance_threshold': 1000, 'zivo_insurance_rate': 1.0,
                            'zivo_insurance_min': 25.0})
        order = self._create_order()
        shipment = self._validate(order.picking_ids).zivo_shipment_id
        self.assertEqual(shipment.quote_count, 3)
        self.assertGreater(shipment.compare_max_price, shipment.quoted_price)
        self.assertAlmostEqual(shipment.savings_amount, shipment.compare_max_price - shipment.quoted_price, places=2)
        self.assertTrue(shipment.insured)
        self.assertAlmostEqual(shipment.insured_value, shipment.declared_value)
        self.assertAlmostEqual(shipment.insurance_cost, max(shipment.declared_value * 0.01, 25.0), places=2)
        self.assertTrue(shipment.on_time_promise)
        # below threshold: not insured
        self.carrier.zivo_insurance_threshold = 100000
        shipment2 = self._validate(self._create_order().picking_ids).zivo_shipment_id
        self.assertFalse(shipment2.insured)
        self.assertEqual(shipment2.insurance_cost, 0.0)
        report = self.env['zivo.logistics.shipment']._read_group([('id', 'in', (shipment | shipment2).ids)], [], ['savings_amount:sum'])
        self.assertAlmostEqual(report[0][0], shipment.savings_amount + shipment2.savings_amount, places=2)

    def test_rule_actions(self):
        self.carrier.zivo_strategy = 'rules'
        self.env['zivo.logistics.allocation.rule'].create({
            'name': 'Metro express insured', 'zone': 'metro', 'strategy': 'cheapest', 'service_code': 'EXP', 'insure': True,
            'account_ids': [(6, 0, self.acc_api.ids)]})
        shipment = self._validate(self._create_order().picking_ids).zivo_shipment_id
        self.assertEqual(shipment.account_id, self.acc_api)
        self.assertEqual(shipment.service_code, 'EXP')
        self.assertTrue(shipment.insured)

    def test_box_suggestion(self):
        to_cm = self.carrier._zivo_to_cm(1.0)
        PackageType = self.env['stock.package.type']
        PackageType.create({'name': 'ZV Small', 'packaging_length': 20 / to_cm, 'width': 20 / to_cm, 'height': 20 / to_cm})
        medium = PackageType.create({'name': 'ZV Medium', 'packaging_length': 30 / to_cm, 'width': 25 / to_cm,
                                     'height': 20 / to_cm})
        PackageType.create({'name': 'ZV Large', 'packaging_length': 60 / to_cm, 'width': 40 / to_cm, 'height': 40 / to_cm})
        volume_uom = self.env['product.template']._get_volume_uom_id_from_ir_config_parameter()
        m3 = self.env.ref('uom.product_uom_cubic_meter')
        self.product.volume = m3._compute_quantity(0.01, volume_uom, round=False)  # 10 litres = 10000 cm3
        order = self._create_order()
        picking = order.picking_ids
        box, info = self.carrier._zivo_suggest_box([(self.product, 1)])
        self.assertEqual(box, medium)  # smallest chargeable weight that fits (Small is too small)
        self.assertGreaterEqual(info['box']['length'] * info['box']['width'] * info['box']['height'], 10000 * 1.15)
        request = self.carrier._zivo_request_from_picking(picking)
        parcel = request.parcels[0]
        self.assertAlmostEqual(parcel.length_cm * parcel.width_cm * parcel.height_cm,
                               info['box']['length'] * info['box']['width'] * info['box']['height'], delta=1)
        self.assertAlmostEqual(info['fill_pct'], 66.7)
        self.assertAlmostEqual(request.volumetric_weight, 3.0, places=2)
        wizard = self.env['zivo.logistics.rate.shop'].browse(picking.action_zivo_rate_shop()['res_id'])
        self.assertIn(box.name, wizard.box_text)
        # a fixed default package always wins over the suggestion
        self.carrier.zivo_default_package_type_id = medium
        self.assertEqual(self.carrier._zivo_box_for([(self.product, 1)]), medium)
        self.carrier.write({'zivo_default_package_type_id': False, 'zivo_auto_box': False})
        self.assertFalse(self.carrier._zivo_box_for([(self.product, 1)]))

    def test_rate_calculator(self):
        wiz = self.env['zivo.logistics.rate.calculator'].create({'carrier_id': self.carrier.id, 'origin_zip': '500051',
                                                                  'dest_zip': '560038', 'weight': 0.8})
        action = wiz.action_compute()
        self.assertEqual(action['res_id'], wiz.id)
        self.assertEqual(len(wiz.line_ids), 3)
        self.assertIn('You save', wiz.summary)
        cheapest = wiz.line_ids.filtered(lambda l: 'Cheapest' in (l.badges or ''))
        self.assertEqual(cheapest.service_code, 'SURF')
        self.assertAlmostEqual(max(wiz.line_ids.mapped('savings')), max(wiz.line_ids.mapped('price')) - cheapest.price, places=2)
        self.assertTrue(all(wiz.line_ids.mapped('edd')))
        self.assertEqual(len(wiz.line_ids.filtered('recommended')), 1)
        wiz.write({'strategy': 'cheapest_on_time', 'deliver_within_days': 2})
        wiz.action_compute()
        self.assertEqual(wiz.line_ids.filtered('recommended').service_code, 'EXP')
        self.assertFalse(wiz.line_ids.filtered(lambda l: l.service_code == 'SURF').on_time)
        wiz.dest_zip = '560000'  # sandbox: not serviceable
        wiz.action_compute()
        self.assertFalse(wiz.line_ids)
        self.assertIn('No carrier', wiz.summary)
        # package type fills the size
        to_cm = self.carrier._zivo_to_cm(1.0)
        pt = self.env['stock.package.type'].create({'name': 'Calc box', 'packaging_length': 40 / to_cm, 'width': 30 / to_cm,
                                                     'height': 20 / to_cm})
        wiz.package_type_id = pt
        wiz._onchange_package_type()
        self.assertEqual((round(wiz.length), round(wiz.width), round(wiz.height)), (40, 30, 20))

    def test_rate_shop_savings(self):
        picking = self._create_order().picking_ids
        wizard = self.env['zivo.logistics.rate.shop'].browse(picking.action_zivo_rate_shop()['res_id'])
        self.assertIn('saves', wizard.savings_text)
        self.assertEqual(min(wizard.line_ids.mapped('savings')), 0.0)
        wizard.write({'strategy': 'cheapest_on_time', 'deliver_within_days': 3})
        wizard.action_compute()
        self.assertEqual(wizard.line_ids.filtered('recommended').service_code, 'STD')


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestOperations(LogisticsCommon):

    def _shipped(self, **kw):
        order = self._create_order(**kw)
        return order, self._validate(order.picking_ids).zivo_shipment_id

    def test_manifest_end_of_day(self):
        self.carrier.write({'zivo_strategy': 'fixed', 'zivo_account_id': self.acc_api.id})
        _o1, s1 = self._shipped()
        _o2, s2 = self._shipped(cod=True)
        Tools = self.env['zivo.logistics.manifest.tools']
        action = Tools.action_create_end_of_day((s1 | s2).ids)
        manifest = self.env['zivo.logistics.manifest'].search(action['domain'])
        self.assertEqual(len(manifest), 1)
        self.assertTrue(manifest.name.startswith('MAN/'))
        self.assertEqual(manifest.shipment_count, 2)
        self.assertEqual(manifest.package_count, 2)
        self.assertAlmostEqual(manifest.cod_total, s2.cod_amount)
        self.assertAlmostEqual(manifest.total_weight, s1.chargeable_weight + s2.chargeable_weight, places=3)
        # already manifested shipments are not added twice
        self.assertEqual(Tools.action_create_end_of_day((s1 | s2).ids)['type'], 'ir.actions.client')
        html = self.env['ir.actions.report']._render_qweb_html('zivoit_logistics_hub.report_manifest', manifest.ids)[0]
        self.assertIn(s1.tracking_number.encode(), html)
        self.assertIn(b'Received by (courier)', html)
        # first print may open the company document layout wizard, then the report itself
        self.assertIn(manifest.action_print()['type'], ('ir.actions.report', 'ir.actions.act_window'))
        pdf = self.env['ir.actions.report']._render_qweb_pdf('zivoit_logistics_hub.report_manifest', manifest.ids)[0]
        self.assertTrue(pdf[:4] in (b'%PDF', b'<!DO', b'<htm') or b'Shipping Manifest' in pdf)
        manifest.action_close()
        self.assertEqual(manifest.state, 'closed')
        self.assertEqual(set((s1 | s2).mapped('state')), {'pickup_scheduled'})
        self.assertTrue(manifest.pickup_reference.startswith('PICKUP-'))
        manifest.action_reopen()
        self.assertEqual(manifest.state, 'draft')

    def test_returns_full_cycle(self):
        order, shipment = self._shipped()
        Return = self.env['zivo.logistics.return']
        returnable = Return._returnable_lines(order)
        self.assertEqual(list(returnable.values()), [1.0])
        line = order.order_line.filtered(lambda l: l.product_id == self.product)
        ret = Return._portal_submit(order, {line.id: 5}, 'damaged', 'Box was crushed', 'refund')
        self.assertTrue(ret.name.startswith('RMA/'))
        self.assertEqual((ret.state, ret.source, ret.line_ids.quantity), ('requested', 'portal', 1.0))  # clipped
        self.assertEqual(ret.picking_id, shipment.picking_id)
        self.assertTrue(ret.activity_ids)
        self.assertAlmostEqual(ret.amount, line.price_reduce_taxinc)
        with self.assertRaises(UserError):  # nothing left to return
            Return._portal_submit(order, {line.id: 1}, 'damaged')
        ret.action_approve()
        self.assertEqual(ret.state, 'pickup')
        self.assertFalse(ret.activity_ids, 'review activity closed on approval')
        self.assertEqual(ret.return_picking_id.picking_type_code, 'incoming')
        self.assertEqual(ret.return_picking_id.move_ids.product_uom_qty, 1.0)
        reverse = ret.shipment_id
        self.assertTrue(reverse.is_return)
        self.assertEqual(reverse.return_request_id, ret)
        self.assertEqual(reverse.origin_zip, '560038')  # picked up at the customer
        self.assertEqual(ret.return_picking_id.carrier_tracking_ref, reverse.tracking_number)
        self.assertTrue(reverse.label_attachment_id)
        ret.action_mark_received()
        self.assertEqual(ret.state, 'received')
        self.assertEqual(ret.return_picking_id.state, 'done')
        with self.assertRaises(UserError):
            ret.action_approve()

    def test_returns_policy_exchange_reject(self):
        (self.acc_api | self.acc_card).write({'extra_config': '{"step_minutes": 600}'})  # pickups stay cancellable
        order, _shipment = self._shipped()
        Return = self.env['zivo.logistics.return']
        line = order.order_line.filtered(lambda l: l.product_id == self.product)
        self.product.product_tmpl_id.zivo_non_returnable = True
        self.assertEqual(Return._returnable_lines(order), {})
        self.product.product_tmpl_id.zivo_non_returnable = False
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('zivoit_logistics_hub.return_window_days', '1')
        order.picking_ids.date_done = fields.Datetime.now() - timedelta(days=3)
        self.assertEqual(Return._returnable_lines(order), {})
        params.set_param('zivoit_logistics_hub.return_window_days', '30')
        # auto approval with exchange and manual pickup booking
        params.set_param('zivoit_logistics_hub.return_auto_approve', 'True')
        params.set_param('zivoit_logistics_hub.return_manual_pickup', 'True')
        ret = Return._portal_submit(order, {line.id: 1}, 'size_fit', resolution='exchange')
        self.assertEqual(ret.state, 'approved')
        self.assertTrue(ret.exchange_picking_id)
        self.assertEqual(ret.exchange_picking_id.picking_type_code, 'outgoing')
        self.assertFalse(ret.shipment_id)
        ret.action_book_pickup()
        self.assertEqual(ret.state, 'pickup')
        ret.action_cancel()
        self.assertEqual(ret.state, 'cancelled')
        self.assertEqual(ret.shipment_id.state, 'cancelled')
        # reason "other" always waits for review; reject cancels nothing booked
        ret2 = Return._portal_submit(order, {line.id: 1}, 'other')
        self.assertEqual(ret2.state, 'requested')
        ret2.action_reject()
        self.assertEqual(ret2.state, 'rejected')
        self.assertIn(ret2.portal_url.rsplit('/', 1)[1], ret2.access_token)
        # back office request
        ret3 = Return.create({'sale_id': order.id, 'partner_id': order.partner_id.id, 'reason': 'quality',
                              'line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1})]})
        self.assertEqual(ret3.source, 'backend')
        with self.assertRaises(ValidationError):
            ret3.line_ids.quantity = -1

    def test_reconciliation_import(self):
        _o1, s1 = self._shipped()
        _o2, s2 = self._shipped()
        _o3, s3 = self._shipped(cod=True)
        s3.write({'state': 'delivered', 'delivered_at': fields.Datetime.now()})
        self.assertEqual(s3.cod_state, 'pending')
        self.assertEqual(s1.reconcile_state, 'pending')
        csv_text = '\n'.join([
            'AWB No;Freight Charges;Charged Weight;COD Remitted;UTR;Remittance Date',
            '%s;%.2f;%.3f;;;' % (s1.tracking_number, s1.quoted_price, s1.chargeable_weight),
            '%s;"%.2f";3.0;;;' % (s2.tracking_number, s2.quoted_price + 50),
            '%s;%.2f;;%.2f;UTR998877;2026-11-10' % (s3.tracking_number, s3.quoted_price, s3.cod_amount - 100),
            'UNKNOWNAWB1;99;;;;',
        ])
        wiz = self.env['zivo.logistics.reconcile'].create({'file': base64.b64encode(csv_text.encode()), 'filename': 'inv.csv'})
        wiz.action_import()
        self.assertEqual(wiz.state, 'done')
        self.assertEqual(s1.reconcile_state, 'matched')
        self.assertEqual(s2.reconcile_state, 'overcharged')
        self.assertAlmostEqual(s2.billing_variance, 50.0, places=2)
        self.assertTrue(s2.weight_dispute)
        self.assertEqual((s3.cod_state, s3.cod_remittance_ref, str(s3.cod_remitted_date)), ('short', 'UTR998877', '2026-11-10'))
        self.assertIn('UNKNOWNAWB1', wiz.result)
        self.assertEqual(len(wiz.matched_ids), 3)
        issues = self.env['zivo.logistics.shipment'].search(wiz.action_view_issues()['domain'])
        self.assertEqual(issues, s2 | s3)
        # full remittance + comma file with a currency symbol
        csv2 = 'tracking_number,cod_remitted,utr\n%s,"Rs 1,999.00",UTR1\n' % s3.tracking_number
        s3.cod_amount = 1999.0
        self.env['zivo.logistics.reconcile'].create({'file': base64.b64encode(csv2.encode())}).action_import()
        self.assertEqual(s3.cod_state, 'remitted')
        with self.assertRaises(UserError):
            self.env['zivo.logistics.reconcile'].create({'file': base64.b64encode(b'foo,bar\n1,2\n')}).action_import()

    def test_scan_to_ship(self):
        order = self._create_order()
        picking = order.picking_ids
        scan = self.env['zivo.logistics.scan.ship'].create({'barcode': picking.name})
        scan.action_scan()
        self.assertEqual(picking.state, 'done')
        shipment = picking.zivo_shipment_id
        self.assertTrue(shipment.tracking_number)
        self.assertIn('OK', scan.log_html)
        self.assertIn(shipment.tracking_number, scan.log_html)
        self.assertEqual(scan.last_label_id, shipment.label_attachment_id)
        self.assertFalse(scan.barcode)
        # re-scan the AWB: found, not booked twice
        scan.barcode = shipment.tracking_number
        scan.action_scan()
        self.assertEqual(len(picking.zivo_shipment_ids), 1)
        # sales order number
        order2 = self._create_order()
        scan.barcode = order2.name
        scan.action_scan()
        self.assertTrue(order2.picking_ids.zivo_shipment_id)
        scan.barcode = 'NO-SUCH-CODE'
        scan.action_scan()
        self.assertIn('ERROR', scan.log_html)
        with self.assertRaises(UserError):
            scan.action_scan()  # empty barcode

    def test_delay_alerts_and_local_cancel(self):
        _o, shipment = self._shipped()
        shipment.write({'state': 'in_transit', 'edd': fields.Datetime.now() - timedelta(days=1)})
        Shipment = self.env['zivo.logistics.shipment']
        self.assertGreaterEqual(Shipment._alert_delays(), 1)
        self.assertTrue(shipment.delay_alerted)
        self.assertTrue(shipment.activity_ids.filtered(lambda a: a.summary == 'Shipment running late'))
        count = len(shipment.activity_ids)
        Shipment._alert_delays()
        self.assertEqual(len(shipment.activity_ids), count)
        _o2, shipment2 = self._shipped()
        with patch.object(DemoProvider, 'cancel', side_effect=NotSupported('Sandbox', 'cancel')):
            shipment2.action_cancel_shipment()
        self.assertEqual(shipment2.state, 'cancelled')
        self.assertIn('no cancellation API', shipment2.message_ids[0].body)

    def test_settings(self):
        settings = self.env['res.config.settings'].create({'zivo_return_window_days': 15, 'zivo_return_auto_approve': True,
                                                           'zivo_audit_tolerance_pct': 5.0})
        settings.execute()
        policy = self.env['zivo.logistics.return']._policy()
        self.assertEqual((policy['window'], policy['auto_approve'], policy['auto_pickup']), (15, True, True))
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param('zivoit_logistics_hub.audit_tolerance_pct'), '5.0')


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestReturnsPortal(HttpCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.partner = env['res.partner'].create({'name': 'Portal Buyer', 'email': 'buyer@example.com', 'phone': '+91 98450 12345',
                                                  'street': 'MG Road 1', 'city': 'Bengaluru', 'zip': '560001',
                                                  'country_id': env.ref('base.in').id})
        self.product = env['product.product'].create({'name': 'Portal Saree', 'type': 'consu', 'list_price': 2500.0})
        self.order = env['sale.order'].create({'partner_id': self.partner.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 2, 'price_unit': 2500.0})]})
        self.order.action_confirm()
        picking = self.order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.move_ids.picked = True
        picking.with_context(skip_sms=True).button_validate()
        self.assertEqual(picking.state, 'done')

    def test_portal_request(self):
        resp = self.url_open('/logistics/returns')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Return or exchange an item', resp.text)
        resp = self.url_open('/logistics/returns', data={'order': self.order.name, 'contact': 'someone@else.com'})
        self.assertIn('could not find an order', resp.text)
        resp = self.url_open('/logistics/returns', data={'order': self.order.name.lower(), 'contact': '12345'})
        self.assertIn('Portal Saree', resp.text)  # phone last digits work too
        resp = self.url_open('/logistics/returns', data={'order': self.order.name, 'contact': 'BUYER@example.com'})
        self.assertIn('Portal Saree', resp.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', resp.text).group(1)
        access = re.search(r'name="access" value="([^"]+)"', resp.text).group(1)
        qty_field = re.search(r'name="(qty_\d+)"', resp.text).group(1)
        # tampered access token
        resp = self.url_open('/logistics/returns/submit', data={'csrf_token': csrf, 'order_id': self.order.id,
                                                                'access': 'bad', qty_field: 1, 'reason': 'damaged'})
        self.assertIn('session expired', resp.text)
        resp = self.url_open('/logistics/returns/submit', data={
            'csrf_token': csrf, 'order_id': self.order.id, 'access': access, qty_field: 1, 'reason': 'damaged',
            'resolution': 'exchange', 'comment': 'Torn at the border'})
        self.assertEqual(resp.status_code, 200)
        ret = self.env['zivo.logistics.return'].search([('sale_id', '=', self.order.id)])
        self.assertEqual(len(ret), 1)
        self.assertIn('/logistics/returns/status/%s' % ret.access_token, resp.url)
        self.assertIn(ret.name, resp.text)
        self.assertIn('Requested', resp.text)
        self.assertEqual((ret.reason, ret.resolution, ret.line_ids.quantity), ('damaged', 'exchange', 1.0))
        # one unit left to return
        resp = self.url_open('/logistics/returns', data={'order': self.order.name, 'contact': 'buyer@example.com'})
        self.assertIn(ret.name, resp.text)
        resp = self.url_open('/logistics/returns/label/%s' % ret.access_token)
        self.assertEqual(resp.status_code, 404)  # no pickup booked yet
        resp = self.url_open('/logistics/returns/status/%s' % ('x' * 32))
        self.assertIn('Return request not found', resp.text)
