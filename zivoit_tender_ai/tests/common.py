# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import MagicMock

from odoo import fields
from odoo.tests import TransactionCase, new_test_user


def fake_response(content=b'', json_data=None, status=200, url='https://example.gov.in/list'):
    response = MagicMock()
    response.status_code = status
    response.content = content if isinstance(content, bytes) else content.encode('utf-8')
    response.text = response.content.decode('utf-8', 'ignore')
    response.url = url
    response.json.return_value = json_data
    response.raise_for_status.return_value = None
    return response


class TenderCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_create_nosubscribe=True))
        cls.Tender = cls.env['zivoit.tender']
        cls.Profile = cls.env['zivoit.tender.profile']
        cls.Source = cls.env['zivoit.tender.source']
        # isolate from demo data
        cls.Tender.search([]).write({'active': False})
        cls.Profile.search([]).write({'active': False})
        cls.env['ir.config_parameter'].sudo().set_param('zivoit_tender_ai.ai_provider', 'none')
        cls.ts = cls.env.ref('base.state_in_ts')
        cls.ap = cls.env.ref('base.state_in_ap')
        cls.scr = cls.env.ref('zivoit_tender_ai.zone_scr')
        cls.sector_rail = cls.env.ref('zivoit_tender_ai.sector_railways')
        cls.sector_road = cls.env.ref('zivoit_tender_ai.sector_roads')
        cls.sector_bldg = cls.env.ref('zivoit_tender_ai.sector_buildings')
        cls.manager = new_test_user(cls.env, login='tender_mgr', name='Tender Manager',
                                    email='mgr@example.com', groups='zivoit_tender_ai.group_tender_manager')
        cls.executive = new_test_user(cls.env, login='tender_exec', name='Tender Exec',
                                      email='exec@example.com', groups='zivoit_tender_ai.group_tender_user')
        cls.now = fields.Datetime.now()

    @classmethod
    def _tender(cls, **vals):
        defaults = {
            'name': 'Construction of ROB at Kazipet',
            'organisation': 'South Central Railway',
            'estimated_value': 18.5e7,
            'emd_amount': 10.75e5,
            'date_closing': cls.now + timedelta(days=12),
            'completion_months': 18,
        }
        defaults.update(vals)
        return cls.Tender.create(defaults)
