# -*- coding: utf-8 -*-
from odoo import fields, models

P = 'zivoit_field_tracking.'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zivo_interval_sec = fields.Integer(string='GPS Interval (seconds)', default=60,
                                       config_parameter=P + 'interval_sec')
    zivo_min_distance_m = fields.Integer(string='Minimum Movement (m)', default=25,
                                         config_parameter=P + 'min_distance_m')
    zivo_max_accuracy_m = fields.Integer(string='Ignore Fixes Worse Than (m)', default=150,
                                         config_parameter=P + 'max_accuracy_m')
    zivo_offline_threshold_min = fields.Integer(string='Offline After Upload Delay (min)', default=3,
                                                config_parameter=P + 'offline_threshold_min')
    zivo_live_minutes = fields.Integer(string='"Live" Within (min)', default=5,
                                       config_parameter=P + 'live_minutes')
    zivo_delayed_minutes = fields.Integer(string='"Delayed" Within (min)', default=30,
                                          config_parameter=P + 'delayed_minutes')
    zivo_geofence_radius_m = fields.Integer(string='Visit Geo-fence Radius (m)', default=200,
                                            config_parameter=P + 'geofence_radius_m')
    zivo_learn_customer_location = fields.Boolean(string='Learn Customer Location From First Visit',
                                                  config_parameter=P + 'learn_customer_location')
    zivo_retention_days = fields.Integer(string='Keep Location History (days)', default=180,
                                         config_parameter=P + 'retention_days')
    zivo_max_speed_kmh = fields.Integer(string='GPS Spike Filter (km/h)', default=250,
                                        config_parameter=P + 'max_speed_kmh')
    zivo_stop_radius_m = fields.Integer(string='Stop Radius (m)', default=100, config_parameter=P + 'stop_radius_m')
    zivo_stop_minutes = fields.Integer(string='Stop Minimum Duration (min)', default=10,
                                       config_parameter=P + 'stop_minutes')
    zivo_tile_url = fields.Char(string='Map Tile URL',
                                default='https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
                                config_parameter=P + 'tile_url')
    zivo_tile_attribution = fields.Char(string='Map Attribution', default='&copy; OpenStreetMap contributors',
                                        config_parameter=P + 'tile_attribution')
