# -*- coding: utf-8 -*-
"""Every Cargo Pilot model must compute its fields for many records at once (list views, exports, API, crons).
Live test, October 2026: trips raised "Expected singleton" when their tracking links were read in a batch."""
from odoo.tests import tagged

from .common_dispatch import DispatchCommon


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestBatchRead(DispatchCommon):

    def test_all_fields_read_in_batch(self):
        # make sure there are several plans and trips (demo data adds more)
        for _i in range(2):
            plan = self._plan()
            plan.action_ready()
            self._bid_and_award(plan, vendor=self.v1, rate=8200)
        failures = []
        checked = 0
        for name in sorted(self.env.registry.models):
            Model = self.env[name]
            if not name.startswith('cp.') or Model._transient or Model._abstract or not Model._auto:
                continue
            records = Model.with_context(active_test=False).search([], limit=30)
            if len(records) < 2:
                continue
            checked += 1
            for fname, field in Model._fields.items():
                if field.type in ('binary',) and not field.store:
                    continue
                try:
                    records.invalidate_recordset()
                    records.mapped(fname)
                except Exception as err:  # noqa: BLE001 - collect every failing field
                    failures.append('%s.%s: %s' % (name, fname, err))
        self.assertGreater(checked, 5)
        self.assertFalse(failures, '\n'.join(failures))
