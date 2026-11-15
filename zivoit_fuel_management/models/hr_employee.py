# -*- coding: utf-8 -*-
import calendar
from datetime import date as dt_date

from odoo import api, fields, models
from odoo.exceptions import UserError

PUMP_ROLES = [('manager', 'Manager'), ('cashier', 'Cashier'), ('dsm', 'Filler / DSM'),
              ('driver', 'Driver'), ('cleaner', 'Cleaner'), ('other', 'Other')]


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    pump_role = fields.Selection(PUMP_ROLES, string='Pump Role')
    pump_basic = fields.Float('Basic (monthly)', digits=(16, 2))
    pump_da = fields.Float('DA (monthly)', digits=(16, 2))
    pump_hra = fields.Float('HRA (monthly)', digits=(16, 2))
    pump_gross = fields.Float('Gross (monthly)', compute='_compute_pump_gross', digits=(16, 2))
    pump_attendance_ids = fields.One2many('pump.attendance', 'employee_id')

    @api.depends('pump_basic', 'pump_da', 'pump_hra')
    def _compute_pump_gross(self):
        for e in self:
            e.pump_gross = e.pump_basic + e.pump_da + e.pump_hra


class PumpAttendance(models.Model):
    _name = 'pump.attendance'
    _description = 'Pump Staff Attendance'
    _order = 'date desc, employee_id'

    date = fields.Date(required=True, default=fields.Date.context_today, index=True)
    employee_id = fields.Many2one('hr.employee', required=True, index=True)
    shift_id = fields.Many2one('pump.shift')
    status = fields.Selection([('present', 'Present'), ('half', 'Half Day'), ('absent', 'Absent'),
                               ('leave', 'Paid Leave'), ('off', 'Weekly Off')], default='present', required=True)
    overtime_hours = fields.Float()
    note = fields.Char()

    _emp_date_uniq = models.Constraint('UNIQUE(employee_id, date)', 'Attendance already marked for this day.')

    @property
    def paid_days(self):
        return {'present': 1.0, 'half': 0.5, 'leave': 1.0, 'off': 1.0}.get(self.status, 0.0)


class PumpSalarySheet(models.Model):
    _name = 'pump.salary.sheet'
    _description = 'Monthly Salary Sheet'
    _inherit = ['mail.thread']
    _order = 'year desc, month desc'

    name = fields.Char(compute='_compute_name', store=True)
    month = fields.Selection([(str(i), calendar.month_name[i]) for i in range(1, 13)], required=True,
                             default=lambda s: str(fields.Date.context_today(s).month))
    year = fields.Integer(required=True, default=lambda s: fields.Date.context_today(s).year)
    line_ids = fields.One2many('pump.salary.line', 'sheet_id', copy=False)
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    total_net = fields.Monetary(compute='_compute_total', store=True)
    pay_account = fields.Selection([('cash', 'Cash'), ('bank', 'Bank')], default='cash', required=True)
    voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Paid'), ('cancel', 'Cancelled')], default='draft')

    _month_uniq = models.Constraint('UNIQUE(month, year)', 'A salary sheet already exists for this month.')

    @api.depends('month', 'year')
    def _compute_name(self):
        for s in self:
            s.name = self.env._('Salary %(m)s %(y)s', m=calendar.month_name[int(s.month)] if s.month else '', y=s.year)

    @api.depends('line_ids.net')
    def _compute_total(self):
        for s in self:
            s.total_net = sum(s.line_ids.mapped('net'))

    def action_compute(self):
        for s in self.filtered(lambda x: x.state == 'draft'):
            month, year = int(s.month), s.year
            days = calendar.monthrange(year, month)[1]
            start, end = dt_date(year, month, 1), dt_date(year, month, days)
            s.line_ids.unlink()
            vals = []
            for emp in self.env['hr.employee'].search([('pump_role', '!=', False)]):
                att = self.env['pump.attendance'].search([('employee_id', '=', emp.id),
                                                         ('date', '>=', start), ('date', '<=', end)])
                paid = sum(a.paid_days for a in att) if att else days
                gross = round(emp.pump_gross * paid / days, 2)
                vals.append((0, 0, {'employee_id': emp.id, 'month_days': days, 'paid_days': paid,
                                    'monthly_gross': emp.pump_gross, 'earned': gross}))
            s.line_ids = vals
        return True

    def action_post(self):
        for s in self.filtered(lambda x: x.state == 'draft'):
            if not s.line_ids:
                raise UserError(self.env._('Compute the salary sheet first.'))
            credit_acc = self.env.ref('zivoit_fuel_management.acc_cash' if s.pay_account == 'cash' else 'zivoit_fuel_management.acc_bank')
            lines = [{'account_id': self.env.ref('zivoit_fuel_management.acc_salary').id, 'debit': s.total_net + sum(s.line_ids.mapped('advance')),
                      'name': s.name}]
            if sum(s.line_ids.mapped('advance')):
                lines.append({'account_id': self.env.ref('zivoit_fuel_management.acc_staff_advance').id,
                              'credit': sum(s.line_ids.mapped('advance')), 'name': self.env._('Advance recovered')})
            lines.append({'account_id': credit_acc.id, 'credit': s.total_net, 'name': s.name})
            voucher = self.env['pump.voucher']._create_posted(
                lines, voucher_type='payment', date=dt_date(s.year, int(s.month), 1).replace(
                    day=calendar.monthrange(s.year, int(s.month))[1]),
                narration=s.name, origin_model=s._name, origin_id=s.id)
            s.write({'voucher_id': voucher.id, 'state': 'posted'})
        return True

    def action_cancel(self):
        for s in self:
            if s.voucher_id:
                s.voucher_id.with_context(pump_from_origin=True).action_cancel()
            s.state = 'cancel'


class PumpSalaryLine(models.Model):
    _name = 'pump.salary.line'
    _description = 'Salary Sheet Line'

    sheet_id = fields.Many2one('pump.salary.sheet', required=True, ondelete='cascade')
    employee_id = fields.Many2one('hr.employee', required=True)
    pump_role = fields.Selection(related='employee_id.pump_role')
    month_days = fields.Integer()
    paid_days = fields.Float(digits=(16, 1))
    monthly_gross = fields.Float(digits=(16, 2))
    earned = fields.Float(digits=(16, 2))
    incentive = fields.Float(digits=(16, 2))
    advance = fields.Float('Advance Deduction', digits=(16, 2))
    net = fields.Float(compute='_compute_net', store=True, digits=(16, 2))

    @api.depends('earned', 'incentive', 'advance')
    def _compute_net(self):
        for l in self:
            l.net = l.earned + l.incentive - l.advance
