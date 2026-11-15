# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    pump_dealer_code = fields.Char('Dealer / SAP Code')
    pump_outlet_name = fields.Char('Retail Outlet Name')
    pump_fy_start_month = fields.Integer('Financial Year Start Month', default=4,
                                         help='4 = April (Indian financial year April-March).')
    pump_billing_days = fields.Integer('Default Billing Period (days)', default=15)
    pump_bill_terms = fields.Text(
        'Bill Terms & Conditions',
        default=(
            "1. Billing period is 15 days.\n"
            "2. Please clear the bill within 5 days of submission, failing which credit may be stopped.\n"
            "3. Discrepancies, if any, must be reported within 3 days of the bill date.\n"
            "4. Interest @18% p.a. may be charged on overdue amounts."
        ))
    pump_bank_name = fields.Char('Bank Name')
    pump_bank_account = fields.Char('Bank A/c No.')
    pump_bank_ifsc = fields.Char('IFSC')
    pump_bank_branch = fields.Char('Bank Branch')
    pump_upi_id = fields.Char('UPI ID')
    pump_ai_enabled = fields.Boolean('AI Assistant in Customer Portal', default=True)
    pump_ai_provider = fields.Selection([('builtin', 'Built-in AI (offline, no data leaves Odoo)'),
                                         ('anthropic', 'Anthropic Claude (API key)'),
                                         ('openai', 'OpenAI (API key)')], default='builtin', string='AI Engine')
    pump_ai_api_key = fields.Char('AI API Key', groups='base.group_system')
    pump_ai_model = fields.Char('AI Model', help='e.g. claude-haiku-5-5 or gpt-4o-mini. Leave empty for the default.')

    def action_open_pump_settings(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.company',
            'res_id': self.env.company.id,
            'view_mode': 'form',
            'views': [(self.env.ref('zivoit_fuel_management.view_company_pump_form').id, 'form')],
            'target': 'current',
            'name': 'Station Settings',
        }
