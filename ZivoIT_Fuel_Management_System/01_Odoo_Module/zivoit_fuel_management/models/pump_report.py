# -*- coding: utf-8 -*-
from odoo import api, fields, models


def _drcr(v):
    v = v or 0.0
    return '%s %s' % ('{:,.2f}'.format(abs(v)), 'Dr' if v >= 0 else 'Cr')


class ReportStatement(models.AbstractModel):
    _name = 'report.zivoit_fuel_management.report_statement'
    _description = 'Customer Statement Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        today = fields.Date.context_today(self)
        d_from = fields.Date.to_date(data.get('date_from')) if data.get('date_from') else today.replace(day=1)
        d_to = fields.Date.to_date(data.get('date_to')) if data.get('date_to') else today
        partners = self.env['res.partner'].browse(docids)
        return {
            'doc_ids': docids, 'doc_model': 'res.partner', 'docs': partners,
            'statements': [p.pump_ledger(d_from, d_to) for p in partners],
            'drcr': _drcr,
        }


class ReportTrialBalance(models.AbstractModel):
    _name = 'report.zivoit_fuel_management.report_trial_balance'
    _description = 'Trial Balance Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        today = fields.Date.context_today(self)
        d_from = fields.Date.to_date(data.get('date_from')) if data.get('date_from') else today.replace(month=1, day=1)
        d_to = fields.Date.to_date(data.get('date_to')) if data.get('date_to') else today
        return {
            'doc_ids': docids, 'doc_model': 'pump.trial.balance.wizard',
            'docs': self.env['pump.trial.balance.wizard'].browse(docids),
            'rows': self.env['pump.trial.balance.wizard'].compute_rows(d_from, d_to),
            'date_from': d_from, 'date_to': d_to, 'drcr': _drcr,
        }
