# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
import unittest

from odoo.modules.registry import Registry
from odoo.tests.common import get_db_name


def accounting_installed():
    return 'account.move' in Registry(get_db_name())


class ZivoMcpSetupMixin:
    """Creates an API-key connector with a dedicated integration user."""

    @classmethod
    def _setup_connector(cls):
        env = cls.env
        group_ids = [env.ref('base.group_user').id, env.ref('base.group_partner_manager').id]
        for xmlid in ('account.group_account_invoice', 'account.group_account_readonly'):
            group = env.ref(xmlid, raise_if_not_found=False)
            if group:
                group_ids.append(group.id)
        cls.integration_user = env['res.users'].create({
            'name': 'AI Integration Test', 'login': 'mcp-test-user',
            'company_id': env.company.id, 'company_ids': [(6, 0, env.company.ids)],
            'group_ids': [(6, 0, group_ids)],
        })
        cls.connector = env['zivo.mcp.connector'].create({
            'name': 'Test connector', 'entity_label': 'Test Entity', 'auth_mode': 'api_key',
            'user_id': cls.integration_user.id, 'company_ids': [(6, 0, env.company.ids)],
        })
        cls.connector.action_activate()
        cls.key = env['res.users.apikeys'].with_user(cls.integration_user).sudo()._generate('mcp', 'test', False)

    def _ctx(self, connector=None):
        from odoo.addons.zivoit_mcp_connector.services.context import ToolContext
        connector = connector or self.connector
        env = self.env(user=connector.user_id.id, context={'allowed_company_ids': connector._effective_company_ids()})
        return ToolContext(env, connector.sudo(), 'http://odoo.test')


try:
    from odoo.addons.account.tests.common import AccountTestInvoicingCommon
except ImportError:  # pragma: no cover
    AccountTestInvoicingCommon = None


class ZivoMcpAccountingCommon(ZivoMcpSetupMixin, AccountTestInvoicingCommon or object):

    @classmethod
    def setUpClass(cls):
        if not accounting_installed():
            raise unittest.SkipTest('Invoicing / Accounting is not installed')
        super().setUpClass()
        cls.env.user.sudo().group_ids = [(4, cls.env.ref('zivoit_mcp_connector.group_zivo_mcp_manager').id)]
        cls._setup_connector()

    @classmethod
    def _post_invoice(cls, move_type, amount, date, partner=None):
        move = cls.env['account.move'].create({
            'move_type': move_type,
            'partner_id': (partner or cls.partner_a).id,
            'invoice_date': date,
            'invoice_line_ids': [(0, 0, {'name': 'line', 'quantity': 1, 'price_unit': amount, 'tax_ids': [(6, 0, [])],
                                         'product_id': cls.product_a.id})],
        })
        move.action_post()
        return move
