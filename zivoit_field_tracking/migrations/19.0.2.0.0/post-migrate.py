# -*- coding: utf-8 -*-
"""19.0.1 -> 19.0.2: field expenses. Give existing day logs a vehicle and compute their allowances."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    cr.execute("UPDATE zivo_track_session SET vehicle_type = 'two_wheeler' WHERE vehicle_type IS NULL")
    env = api.Environment(cr, SUPERUSER_ID, {})
    sessions = env['zivo.track.session'].search([])
    for start in range(0, len(sessions), 500):
        sessions[start:start + 500]._compute_allowances()
