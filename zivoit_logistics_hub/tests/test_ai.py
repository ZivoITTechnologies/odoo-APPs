# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from ..ai import engine
from ..ai.india import state_matches, states_for_pincode, zone_for
from ..ai.llm import LLMClient, extract_json
from .common import LogisticsCommon, carrier_server, fake_response


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestAIEngine(LogisticsCommon):

    def test_india_postal_intelligence(self):
        self.assertEqual(states_for_pincode('500051'), {'TS'})
        self.assertEqual(states_for_pincode('403001'), {'GA'})
        self.assertEqual(states_for_pincode('795001'), {'MN'})
        self.assertTrue(state_matches('560038', 'KA'))
        self.assertFalse(state_matches('560038', 'TS'))
        self.assertIsNone(state_matches('abc', 'TS'))
        self.assertEqual(zone_for('500051', '500032'), 'local')
        self.assertEqual(zone_for('500051', '506002'), 'regional')
        self.assertEqual(zone_for('500051', '560038'), 'metro')
        self.assertEqual(zone_for('500051', '302001'), 'national')
        self.assertEqual(zone_for('500051', '781001'), 'special')

    def test_address_assessment(self):
        good = engine.assess_address({'name': 'Ravi Kumar', 'street': 'Flat 402, Sai Residency, 4th Cross',
                                      'street2': 'Near Metro Station', 'city': 'Bengaluru', 'state_code': 'KA',
                                      'zip': '560038', 'phone': '+919848022338', 'email': 'r@x.com'})
        self.assertGreaterEqual(good['score'], 95)
        bad = engine.assess_address({'name': 'xx', 'street': 'asdf', 'city': '', 'state_code': 'TS', 'zip': '12345',
                                     'phone': '12345'})
        self.assertLess(bad['score'], 20)
        messages = ' '.join(m for _s, m in bad['issues'])
        self.assertIn('Invalid Indian PIN', messages)
        self.assertIn('not a valid', messages)
        mismatch = engine.assess_address({'name': 'Asha', 'street': 'H.No 5-9, Ameerpet Road, near Metro', 'city': 'Hyderabad',
                                          'state_code': 'KA', 'zip': '500016', 'phone': '9876501234'})
        self.assertTrue(any('does not belong' in m for _s, m in mismatch['issues']))

    def test_phone_validation(self):
        self.assertTrue(engine.phone_is_valid('+91-98480 22338'))
        self.assertTrue(engine.phone_is_valid('09848022338'))
        self.assertFalse(engine.phone_is_valid('9999999999'))
        self.assertFalse(engine.phone_is_valid('5848022338'))

    def test_rto_risk_is_monotonic_and_explained(self):
        base = {'is_cod': False, 'order_value': 900, 'address_score': 95, 'phone_valid': True, 'customer_delivered': 3}
        low = engine.rto_risk(base)
        risky = engine.rto_risk(dict(base, is_cod=True, customer_delivered=0, customer_rto=2, address_score=40,
                                     phone_valid=False, order_value=8000, pincode_rto_rate=0.35, pincode_samples=60))
        self.assertEqual(low['level'], 'low')
        self.assertEqual(risky['level'], 'high')
        self.assertGreater(risky['score'], low['score'])
        self.assertTrue(any('RTO' in r for r in risky['reasons']))
        cod_only = engine.rto_risk(dict(base, is_cod=True, customer_delivered=0))
        self.assertGreater(cod_only['score'], low['score'])

    def test_rate_scoring_prefers_reliability_for_cod(self):
        options = [
            {'key': 'cheap', 'price': 70, 'tat_days': 4, 'perf': {'samples': 80, 'delivered_rate': 0.68, 'rto_rate': 0.28}},
            {'key': 'good', 'price': 82, 'tat_days': 4, 'perf': {'samples': 80, 'delivered_rate': 0.96, 'rto_rate': 0.02}},
        ]
        cheapest = engine.score_rates([dict(o) for o in options], strategy='cheapest')
        self.assertEqual(cheapest[0]['key'], 'cheap')
        ai_cod = engine.score_rates([dict(o) for o in options], strategy='ai_best', is_cod=True)
        self.assertEqual(ai_cod[0]['key'], 'good')
        self.assertEqual(ai_cod[0]['rank'], 1)
        fastest = engine.score_rates([{'key': 'a', 'price': 50, 'tat_days': 6}, {'key': 'b', 'price': 90, 'tat_days': 2}],
                                     strategy='fastest')
        self.assertEqual(fastest[0]['key'], 'b')

    def test_status_and_ndr_classifiers(self):
        cases = {
            'Shipment Delivered': 'delivered', 'Not Delivered - Customer refused': 'ndr', 'RTO Delivered to seller': 'rto_delivered',
            'Out For Delivery': 'out_for_delivery', 'Arrived at Destination Hub': 'in_transit', 'Shipment cancelled by seller': 'cancelled',
            'Pickup Scheduled': 'pickup_scheduled', 'Return to origin initiated': 'rto_initiated', 'Undelivered - door locked': 'ndr',
        }
        for text, expected in cases.items():
            self.assertEqual(engine.classify_status(text)[0], expected, text)
        self.assertEqual(engine.classify_ndr('Customer refused to accept')['action'], 'rto')
        self.assertEqual(engine.classify_ndr('COD amount not ready')['category'], 'cod_not_ready')
        self.assertEqual(engine.classify_ndr('Incomplete address, unable to locate')['action'], 'change_address')
        self.assertEqual(engine.classify_ndr('Door locked')['category'], 'customer_unavailable')
        self.assertIn('ORD1', engine.customer_message('address_issue', 'Ravi Kumar', 'ORD1'))

    def test_edd_and_weight(self):
        days, conf, basis = engine.predict_tat([2, 3, 3, 4, 2, 5, 3, 3], carrier_tat=5, zone='metro')
        self.assertEqual(days, 3)
        self.assertIn('p75', basis)
        self.assertEqual(engine.predict_tat([], carrier_tat=0, zone='special')[0], 7.0)
        sat = datetime(2026, 11, 7, 10, 0)  # Saturday
        self.assertEqual(engine.add_business_days(sat, 1).weekday(), 0)  # skips Sunday
        self.assertTrue(engine.weight_dispute(0.45, 1.0))
        self.assertFalse(engine.weight_dispute(0.45, 0.5))

    def test_nl_parser(self):
        out = engine.parse_nl_query('Delayed COD shipments to Hyderabad this week via Delhivery', ['Delhivery', 'DTDC'],
                                    now=datetime(2026, 11, 6, 12))
        self.assertTrue(out['delayed'])
        self.assertTrue(out['cod'])
        self.assertEqual(out['city'], 'hyderabad')
        self.assertEqual(out['carriers'], ['Delhivery'])
        self.assertEqual(out['date_from'], datetime(2026, 10, 30, 12))
        self.assertIn('rto_delivered', engine.parse_nl_query('show RTO orders')['states'])
        self.assertNotIn('delivered', engine.parse_nl_query('not delivered parcels')['states'])

    def test_llm_client_parsing(self):
        self.assertEqual(extract_json('Sure!\n```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(extract_json('result: {"b": [1,2]} thanks'), {'b': [1, 2]})
        with carrier_server([]):
            pass
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append((url, headers, json))
            return fake_response(200, {'content': [{'type': 'text', 'text': '{"status": "ndr", "confidence": 0.9}'}]})
        with patch('odoo.addons.zivoit_logistics_hub.ai.llm.requests.post', side_effect=fake_post):
            out = LLMClient('anthropic', 'k', model='claude-test').complete('sys', 'usr')
        self.assertEqual(out['status'], 'ndr')
        self.assertEqual(calls[0][1]['x-api-key'], 'k')
        self.assertEqual(calls[0][2]['model'], 'claude-test')


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestAIService(LogisticsCommon):

    def _shipment(self, account, state, zip_code='560038', days=3, partner=None, **kw):
        booked = fields.Datetime.now() - timedelta(days=10)
        vals = {'account_id': account.id, 'state': state, 'tracking_number': 'T%s' % self.env['ir.sequence'].next_by_code('zivo.logistics.shipment'),
                'dest_zip': zip_code, 'dest_state_id': self.state_ka.id, 'booked_at': booked,
                'partner_id': (partner or self.customer).id}
        if state == 'delivered':
            vals['delivered_at'] = booked + timedelta(days=days)
        vals.update(kw)
        return self.env['zivo.logistics.shipment'].create(vals)

    def test_status_map_learning(self):
        Map = self.env['zivo.logistics.status.map']
        status, mapping = Map._resolve('dtdc', 'SHIPMENT HANDED OVER TO DELIVERY BOY', '')
        self.assertEqual(status, 'out_for_delivery')
        self.assertFalse(mapping.verified)
        self.assertEqual(mapping.source, 'ai')
        mapping.normalized_status = 'in_transit'  # ops team corrects it
        self.assertTrue(mapping.verified)
        self.assertEqual(Map._resolve('dtdc', 'shipment handed over to delivery boy')[0], 'in_transit')
        # seed mapping applies to every carrier
        self.assertEqual(Map._resolve('anything', 'Delivered')[0], 'delivered')

    def test_lane_performance_and_recommendation(self):
        for _i in range(8):
            self._shipment(self.acc_card, 'delivered', days=2)
        for _i in range(6):
            self._shipment(self.acc_api, 'rto_delivered')
        for _i in range(2):
            self._shipment(self.acc_api, 'delivered', days=4)
        perf = self.env['zivo.logistics.ai']._lane_performance(self.acc_api | self.acc_card, '560038', self.state_ka)
        self.assertEqual(perf[self.acc_card.id]['level'], 'pincode')
        self.assertAlmostEqual(perf[self.acc_api.id]['rto_rate'], 0.75)
        order = self.env['sale.order'].create({'partner_id': self.customer.id, 'order_line': [
            (0, 0, {'product_id': self.product.id, 'product_uom_qty': 1, 'price_unit': 1499.0})]})
        order.zivo_payment_mode = 'cod'
        request = self.carrier._zivo_request_from_order(order)
        ranked, errors = self.carrier._zivo_ranked_options(request, self.carrier._zivo_ctx(self.customer, self.env.company))
        self.assertFalse(errors)
        self.assertEqual(ranked[0]['account'], self.acc_card, 'reliable carrier must win for COD on this lane')
        self.assertTrue(any('RTO' in r for r in ranked[0]['reasons']))

    def test_risk_values_use_history(self):
        ai = self.env['zivo.logistics.ai']
        clean = ai._risk_values(self.customer, 1499.0, False)
        for _i in range(3):
            self._shipment(self.acc_api, 'rto_delivered', partner=self.customer)
        risky = ai._risk_values(self.customer, 1499.0, True)
        self.assertGreater(risky['rto_risk'], clean['rto_risk'])
        self.assertIn('previous RTO', risky['rto_risk_reasons'])
        bad = ai._risk_values(self.bad_customer, 1499.0, True)
        self.assertLess(bad['address_score'], 30)
        self.assertEqual(bad['rto_risk_level'], 'high')

    def test_predicted_edd(self):
        for days in (2, 2, 3, 3, 3, 4):
            self._shipment(self.acc_api, 'delivered', days=days)
        days, conf, basis = self.env['zivo.logistics.ai']._predict_tat(self.acc_api, '560038', self.state_ka, 'metro')
        self.assertEqual(days, 3.0)
        self.assertIn('6 delivered', basis)

    def test_nl_domain(self):
        delayed = self._shipment(self.acc_api, 'in_transit', payment_mode='cod', dest_city='Bengaluru',
                                 edd=fields.Datetime.now() - timedelta(days=1))
        on_time = self._shipment(self.acc_api, 'in_transit', payment_mode='cod', dest_city='Bengaluru',
                                 edd=fields.Datetime.now() + timedelta(days=2))
        domain, filters = self.env['zivo.logistics.ai']._nl_domain('delayed COD shipments to Bengaluru')
        found = self.env['zivo.logistics.shipment'].search(domain)
        self.assertIn(delayed, found)
        self.assertNotIn(on_time, found)
        wizard = self.env['zivo.logistics.ai.query'].create({'question': 'delayed cod shipments'})
        action = wizard.action_search()
        self.assertEqual(action['res_model'], 'zivo.logistics.shipment')

    def test_llm_used_when_configured(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('zivoit_logistics_hub.ai_provider', 'openai')
        params.set_param('zivoit_logistics_hub.ai_api_key', 'sk-test')
        shipment = self._shipment(self.acc_api, 'ndr', payment_mode='cod', cod_amount=999.0)

        def fake_post(url, headers=None, json=None, timeout=None):
            return fake_response(200, {'choices': [{'message': {'content':
                '{"category": "cod_not_ready", "action": "reattempt", "advice": "Share UPI link", "message": "Hi Ravi, pay via UPI"}'}}]})
        ai = self.env['zivo.logistics.ai'].with_context(zivo_allow_llm_in_test=True)
        with patch('odoo.addons.zivoit_logistics_hub.ai.llm.requests.post', side_effect=fake_post):
            out = ai._ndr_assist(shipment, 'Customer said cash not available')
        self.assertEqual(out['category'], 'cod_not_ready')
        self.assertEqual(out['message'], 'Hi Ravi, pay via UPI')
        # LLM failure -> silent fallback to local engine
        with patch('odoo.addons.zivoit_logistics_hub.ai.llm.requests.post', side_effect=Exception('down')):
            out = ai._ndr_assist(shipment, 'Door locked')
        self.assertEqual(out['category'], 'customer_unavailable')
