# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.cargo_pilot_tms.tests.common import PIXEL


class DispatchCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        india = env.ref('base.in')
        State = env['res.country.state']

        def state(code, name):
            return State.search([('country_id', '=', india.id), ('code', '=', code)], limit=1) or \
                State.create({'name': name, 'code': code, 'country_id': india.id})
        cls.st_ct, cls.st_ts = state('CT', 'Chhattisgarh'), state('TS', 'Telangana')
        cls.company = env.company
        cls.company.partner_id.write({'state_id': cls.st_ct.id, 'city': 'Raipur', 'zip': '492001'})
        cls.company.write({'cp_dispatch_sms': False, 'cp_detention_free_hours': 2.0})
        env['cp.rate.contract'].search([('state', '=', 'active')]).write({'state': 'expired'})
        env['cp.channel.rule'].search([]).write({'active': False})
        env['res.partner'].search([('cp_is_3pl_vendor', '=', True)]).write({'cp_is_3pl_vendor': False})
        cls.warehouse = env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.warehouse.partner_id = cls.company.partner_id
        cls.customer = env['res.partner'].create({
            'name': 'Hyderabad Distributor', 'state_id': cls.st_ts.id, 'city': 'Hyderabad', 'zip': '500032',
            'email': 'buyer@example.com', 'phone': '+91 90000 12345'})
        cls.vt_small = env['cp.vehicle.type'].create({'name': 'Tata Ace (0.75 t)', 'capacity_ton': 0.75,
                                                      'length_m': 2.2, 'width_m': 1.4, 'height_m': 1.5})
        cls.vt_14ft = env['cp.vehicle.type'].create({'name': '14 ft (4 t)', 'capacity_ton': 4,
                                                     'length_m': 4.3, 'width_m': 2.0, 'height_m': 2.0})
        cls.vt_32ft = env['cp.vehicle.type'].create({'name': '32 ft MXL (15 t)', 'capacity_ton': 15,
                                                     'length_m': 9.75, 'width_m': 2.4, 'height_m': 2.6})
        cls.product = env['product.product'].create({'name': 'Chimney 90 cm', 'is_storable': True, 'weight': 18.0,
                                                     'volume': 0.12, 'list_price': 14000, 'standard_price': 9000})
        cls.dhl = env.ref('cargo_pilot_tms.partner_carrier_dhl')
        cls.delhivery = env.ref('cargo_pilot_tms.partner_carrier_delhivery')
        cls.fedex = env.ref('cargo_pilot_tms.partner_carrier_fedex')
        (cls.dhl | cls.delhivery | cls.fedex).write({'cp_max_weight_kg': 0, 'cp_service_pincodes': False,
                                                      'cp_max_volume_cbm': 0})
        env['res.partner'].search([('cp_regular_carrier', '=', True)]).filtered(
            lambda p: p not in (cls.dhl | cls.delhivery | cls.fedex)).write({'cp_regular_carrier': False})
        Partner = env['res.partner']
        cls.v1 = Partner.create({'name': 'Vega 3PL', 'is_company': True, 'cp_is_3pl_vendor': True,
                                 'email': 'vega@example.com', 'cp_vehicle_type_ids': [(6, 0, cls.vt_14ft.ids)]})
        cls.v2 = Partner.create({'name': 'Orbit 3PL', 'is_company': True, 'cp_is_3pl_vendor': True,
                                 'email': 'orbit@example.com', 'cp_service_state_ids': [(6, 0, cls.st_ts.ids)]})
        cls.v3 = Partner.create({'name': 'North Only 3PL', 'is_company': True, 'cp_is_3pl_vendor': True,
                                 'cp_service_state_ids': [(6, 0, cls.st_ct.ids)]})
        env['cp.detention.term'].create({'partner_id': cls.v1.id, 'free_hours': 2, 'method': 'hourly',
                                         'hourly_rate': 300})
        env['cp.detention.term'].create({'partner_id': cls.v2.id, 'free_hours': 4, 'method': 'daily',
                                         'daily_rate': 2500, 'partial_day': 'full'})
        cls.ops_head = new_test_user(env, 'cp_ops_head', groups='base.group_user,cargo_pilot_tms.group_dispatch_ops_head')
        cls.acc_manager = new_test_user(env, 'cp_acc_mgr', groups='account.group_account_manager,cargo_pilot_tms.group_cp_user')

    # ------------------------------------------------------------------
    def _sale(self, qty=10):
        order = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': qty, 'price_unit': 14000})]})
        order.action_confirm()
        return order

    def _plan(self, qty=10, **vals):
        order = self._sale(qty)
        values = {'picking_ids': [(6, 0, order.picking_ids.ids)], 'warehouse_id': self.warehouse.id,
                  'partner_id': self.customer.id, 'target_date': fields.Datetime.now() + timedelta(days=3)}
        values.update(vals)
        return self.env['cp.dispatch.plan'].create(values)

    def _log_all_unavailable(self, plan):
        for carrier, status in ((self.dhl, 'no_vehicle'), (self.delhivery, 'over_capacity'),
                                (self.fedex, 'no_service')):
            plan.cp_log_check(carrier, status)

    def _bid_and_award(self, plan, vendor=None, rate=8200.0, mode='reverse_auction', target=0.0):
        self._log_all_unavailable(plan)
        auction = plan.cp_launch_bid(self.v1 | self.v2, 15, mode=mode, target_price=target,
                                     vehicle_type=self.vt_14ft)
        auction.cp_place_bid(vendor or self.v1, rate if mode == 'reverse_auction' else target)
        if auction.state == 'live':
            auction.write({'date_start': fields.Datetime.now() - timedelta(minutes=30), 'date_end': fields.Datetime.now() - timedelta(seconds=1)})
            self.env['cp.auction']._cron_auction_lifecycle()
        plan.cp_award(auction.proposed_bid_id)
        return auction

    def _through_gate(self, plan, waited_hours=0.0, eway='391234567890'):
        action = plan.action_new_gate_entry()
        entry = self.env['cp.gate.entry'].browse(action['res_id'])
        entry.write({'vehicle_number': 'CG04 AB 1234', 'driver_name': 'Ramesh', 'driver_phone': '9000000001',
                     'driver_license': 'CG0420190001234'})
        entry.action_gate_in()
        entry.write({'check_license': True, 'check_fitness': True, 'check_clean': True, 'check_seal': True})
        entry.action_to_dock()
        entry.action_loading_done()
        entry.eway_bill_no = eway
        if waited_hours:
            entry.gate_in_time = fields.Datetime.now() - timedelta(hours=waited_hours)
        entry.action_gate_out()
        return entry

    def _verified_pod(self, plan):
        attachment = self.env['ir.attachment'].create({'name': 'pod.png', 'datas': PIXEL})
        pod = self.env['cp.dispatch.pod'].create({'plan_id': plan.id, 'attachment_ids': [(6, 0, attachment.ids)],
                                                  'check_signature': True, 'check_date': True, 'check_stamp': True,
                                                  'check_quantity': True})
        pod.action_verify()
        return pod
