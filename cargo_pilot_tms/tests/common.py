# -*- coding: utf-8 -*-
import base64
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase

PIXEL = base64.b64encode(
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
    b'\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82')


class CpCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.company = env.company
        # tests build their own contracts and rules, so demo data must not compete with them
        env['cp.rate.contract'].search([('state', '=', 'active')]).write({'state': 'expired'})
        env['cp.channel.rule'].search([]).write({'active': False})
        cls.company.write({'cp_audit_distance_tolerance': 5.0, 'cp_audit_weight_tolerance': 1.0,
                           'cp_auction_extension_minutes': 5, 'cp_epod_geofence_m': 1000})
        Loc = env['cp.location']
        cls.origin = Loc.create({'name': 'Test Plant', 'code': 'TPL', 'location_type': 'plant',
                                 'latitude': 21.2514, 'longitude': 81.6296, 'geofence_radius': 1500})
        cls.dest = Loc.create({'name': 'Test Depot', 'code': 'TDP', 'location_type': 'warehouse',
                               'latitude': 17.3850, 'longitude': 78.4867})
        cls.hub_a = Loc.create({'name': 'Hub A', 'code': 'HA', 'location_type': 'hub'})
        cls.hub_b = Loc.create({'name': 'Hub B', 'code': 'HB', 'location_type': 'hub'})
        cls.vtype = env['cp.vehicle.type'].create({'name': 'Test 32ft', 'capacity_ton': 15,
                                                   'length_m': 9.75, 'width_m': 2.4, 'height_m': 2.6})
        cls.lane = env['cp.lane'].create({'origin_id': cls.origin.id, 'destination_id': cls.dest.id,
                                          'distance_km': 785, 'transit_hours': 30})
        Partner = env['res.partner']
        cls.tr_a = Partner.create({'name': 'Alpha Transport', 'is_company': True, 'cp_is_transporter': True})
        cls.tr_b = Partner.create({'name': 'Beta Logistics', 'is_company': True, 'cp_is_transporter': True})
        cls.tr_c = Partner.create({'name': 'Gamma Carriers', 'is_company': True, 'cp_is_transporter': True})
        cls.customer = Partner.create({'name': 'Test Customer', 'is_company': True})
        cls.contract = env['cp.rate.contract'].create({
            'transporter_id': cls.tr_a.id,
            'date_start': fields.Date.today() - timedelta(days=10),
            'date_end': fields.Date.today() + timedelta(days=200),
            'line_ids': [(0, 0, {'lane_id': cls.lane.id, 'vehicle_type_id': cls.vtype.id, 'charge_basis': 'trip',
                                 'rate': 50000, 'contracted_km': 785, 'detention_per_day': 2000})],
        })
        cls.contract.action_activate()

    def _indent(self, count=2, weight=28.0):
        indent = self.env['cp.indent'].create({
            'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id, 'vehicle_count': count,
            'consignee_id': self.customer.id,
            'line_ids': [(0, 0, {'description': 'Cement', 'quantity': 100, 'weight_ton': weight})],
        })
        indent.action_confirm()
        return indent

    def _delivered_trip(self, transporter=None, with_epod=True, gps_km=None):
        trip = self.env['cp.trip'].create({
            'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
            'transporter_id': (transporter or self.tr_a).id, 'vehicle_number': 'CG04 AA 1111',
            'loaded_weight_ton': 14.0, 'agreed_freight': 50000,
            'contract_line_id': self.contract.line_ids[:1].id,
        })
        trip.action_assign()
        trip.action_dispatch()
        if gps_km is not None:
            trip.gps_distance_km = gps_km
        if with_epod:
            epod = self.env['cp.epod'].create({
                'trip_id': trip.id, 'receiver_name': 'Stores', 'received_weight_ton': 14.0,
                'latitude': self.dest.latitude, 'longitude': self.dest.longitude, 'signature': PIXEL,
                'photo_ids': [(0, 0, {'name': 'pod.png', 'datas': PIXEL})],
            })
            epod.action_submit()
        else:
            trip.action_mark_delivered()
        return trip
