# -*- coding: utf-8 -*-
import math

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two coordinates."""
    if not all(v is not None for v in (lat1, lon1, lat2, lon2)):
        return 0.0
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class CpLocation(models.Model):
    _name = 'cp.location'
    _description = 'Transport Location'
    _order = 'name'

    name = fields.Char(required=True)
    code = fields.Char()
    location_type = fields.Selection([
        ('plant', 'Plant / Factory'),
        ('warehouse', 'Warehouse / Depot'),
        ('hub', 'Transporter Hub / Branch'),
        ('customer', 'Customer Site'),
        ('port', 'Port / Rail Yard'),
        ('other', 'Other'),
    ], default='warehouse', required=True)
    partner_id = fields.Many2one('res.partner', string='Address / Contact')
    city = fields.Char()
    state_id = fields.Many2one('res.country.state', string='State')
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    geofence_radius = fields.Integer(string='Geofence Radius (m)', default=1000)
    is_oda = fields.Boolean(string='Out of Delivery Area (ODA)',
                            help='LTL carriers add an ODA charge for deliveries here.')
    pincode = fields.Char(string='PIN Code')
    dock_ids = fields.One2many('cp.dock', 'location_id', string='Docks')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    def cp_contains(self, lat, lon, radius_m=None):
        """True when (lat, lon) is inside this location's geofence."""
        self.ensure_one()
        if not (self.latitude or self.longitude) or lat is None or lon is None:
            return False
        radius = (radius_m or self.geofence_radius or 1000) / 1000.0
        return haversine_km(self.latitude, self.longitude, lat, lon) <= radius


class CpVehicleType(models.Model):
    _name = 'cp.vehicle.type'
    _description = 'Vehicle Type'
    _order = 'capacity_ton, name'

    name = fields.Char(required=True)
    body_type = fields.Selection([
        ('open', 'Open Body'),
        ('closed', 'Closed Container'),
        ('trailer', 'Flatbed Trailer'),
        ('tanker', 'Tanker'),
        ('reefer', 'Refrigerated'),
        ('bulker', 'Bulker'),
        ('tipper', 'Tipper'),
    ], default='closed', required=True)
    capacity_ton = fields.Float(string='Capacity (t)', required=True, default=9.0)
    length_m = fields.Float(string='Length (m)', default=7.3)
    width_m = fields.Float(string='Width (m)', default=2.4)
    height_m = fields.Float(string='Height (m)', default=2.4)
    volume_cbm = fields.Float(string='Volume (m³)', compute='_compute_volume', store=True)
    emission_factor = fields.Float(string='Emission Factor (g CO2e / t-km)', default=80.0,
                                   help='Use your measured value or a GLEC default for this truck class.')
    active = fields.Boolean(default=True)

    @api.depends('length_m', 'width_m', 'height_m')
    def _compute_volume(self):
        for rec in self:
            rec.volume_cbm = rec.length_m * rec.width_m * rec.height_m

    @api.constrains('capacity_ton', 'length_m', 'width_m', 'height_m')
    def _check_dims(self):
        for rec in self:
            if min(rec.capacity_ton, rec.length_m, rec.width_m, rec.height_m) < 0:
                raise ValidationError(_('Vehicle dimensions and capacity cannot be negative.'))


class CpLane(models.Model):
    _name = 'cp.lane'
    _description = 'Lane (Origin to Destination)'
    _order = 'name'

    name = fields.Char(compute='_compute_name', store=True, readonly=False)
    origin_id = fields.Many2one('cp.location', required=True)
    destination_id = fields.Many2one('cp.location', required=True)
    distance_km = fields.Float(string='Distance (km)')
    transit_hours = fields.Float(string='Standard Transit (h)')
    movement = fields.Selection([('intrastate', 'Intrastate'), ('interstate', 'Interstate')],
                                compute='_compute_movement', store=True,
                                help='Interstate when origin and destination are in different states.')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.depends('origin_id.state_id', 'destination_id.state_id')
    def _compute_movement(self):
        for lane in self:
            o, d = lane.origin_id.state_id, lane.destination_id.state_id
            lane.movement = ('interstate' if o != d else 'intrastate') if o and d else False

    _lane_uniq = models.Constraint(
        'unique(origin_id, destination_id, company_id)',
        'This lane already exists.',
    )

    @api.depends('origin_id', 'destination_id')
    def _compute_name(self):
        for lane in self:
            if lane.origin_id and lane.destination_id:
                lane.name = '%s → %s' % (lane.origin_id.name, lane.destination_id.name)
            else:
                lane.name = lane.name or _('New Lane')

    @api.onchange('origin_id', 'destination_id')
    def _onchange_points(self):
        for lane in self:
            o, d = lane.origin_id, lane.destination_id
            if o and d and (o.latitude or o.longitude) and (d.latitude or d.longitude) and not lane.distance_km:
                # Road distance is roughly 1.25x the straight line on Indian highways.
                lane.distance_km = round(haversine_km(o.latitude, o.longitude, d.latitude, d.longitude) * 1.25)
                speed = self.env.company.cp_default_speed_kmph or 40.0
                lane.transit_hours = round(lane.distance_km / speed, 1)

    @api.constrains('origin_id', 'destination_id')
    def _check_points(self):
        for lane in self:
            if lane.origin_id == lane.destination_id:
                raise ValidationError(_('Origin and destination must be different.'))
