# -*- coding: utf-8 -*-
"""Cargo Pilot 19.0.3: the seven add-on modules are now part of cargo_pilot_tms.

On a database that runs the separate modules (19.0.2.x / 19.0.1.x), their records, fields, views and
access rules are handed over to cargo_pilot_tms before it loads, so nothing is recreated or lost.
Remove the old module folders from the addons path before upgrading.
"""
import logging

_logger = logging.getLogger(__name__)

CORE = 'cargo_pilot_tms'
MERGED = ['cargo_pilot_courier', 'cargo_pilot_courier_delivery', 'cargo_pilot_india', 'cargo_pilot_zoho',
          'cargo_pilot_easyecom', 'cargo_pilot_stock', 'cargo_pilot_dispatch']


def migrate(cr, version):
    cr.execute("SELECT id, name FROM ir_module_module WHERE name = ANY(%s) AND state IN ('installed', 'to upgrade')",
               [MERGED])
    old = cr.fetchall()
    if not old:
        return
    old_ids, old_names = [r[0] for r in old], [r[1] for r in old]
    cr.execute("SELECT id FROM ir_module_module WHERE name = %s", [CORE])
    core_id = cr.fetchone()[0]

    # External ids: the same name can exist in several modules (for example model_cp_trip) -> keep one.
    cr.execute("""DELETE FROM ir_model_data d USING ir_model_data k
                   WHERE d.module = ANY(%s) AND k.module = %s AND k.name = d.name""", [old_names, CORE])
    cr.execute("""DELETE FROM ir_model_data d USING ir_model_data k
                   WHERE d.module = ANY(%s) AND k.module = ANY(%s) AND k.name = d.name AND k.id < d.id""",
               [old_names, old_names])
    cr.execute("UPDATE ir_model_data SET module = %s WHERE module = ANY(%s)", [CORE, old_names])
    moved = cr.rowcount

    # SQL constraints and many2many tables owned by the old modules
    for table in ('ir_model_constraint', 'ir_model_relation'):
        cr.execute("""DELETE FROM {t} d USING {t} k
                       WHERE d.module = ANY(%s) AND k.module = %s AND k.name = d.name""".format(t=table),
                   [old_ids, core_id])
        cr.execute("UPDATE {t} SET module = %s WHERE module = ANY(%s)".format(t=table), [core_id, old_ids])

    # QWeb template keys (portal pages) follow the external id
    cr.execute("""UPDATE ir_ui_view SET key = %s || substring(key FROM position('.' IN key))
                   WHERE split_part(key, '.', 1) = ANY(%s)""", [CORE, old_names])

    # The old modules are no longer separate apps
    cr.execute("DELETE FROM ir_module_module_dependency WHERE module_id = ANY(%s)", [old_ids])
    cr.execute("UPDATE ir_module_module SET state = 'uninstalled', latest_version = NULL WHERE id = ANY(%s)",
               [old_ids])
    _logger.info('Cargo Pilot: merged %s into %s (%s external ids moved).', ', '.join(old_names), CORE, moved)
