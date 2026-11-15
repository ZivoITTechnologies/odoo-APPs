# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class PumpShift(models.Model):
    _name = 'pump.shift'
    _description = 'Shift'
    _order = 'sequence'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=1)
    start_time = fields.Float('Starts At', help='24h clock, e.g. 6.0 = 06:00')
    end_time = fields.Float('Ends At')
    active = fields.Boolean(default=True)


class PumpShiftSheet(models.Model):
    _name = 'pump.shift.sheet'
    _description = 'Shift Sheet (Nozzle Readings & Cash)'
    _inherit = ['mail.thread']
    _order = 'date desc, shift_sequence desc, id desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    shift_id = fields.Many2one('pump.shift', required=True)
    shift_sequence = fields.Integer(related='shift_id.sequence', store=True)
    manager_id = fields.Many2one('hr.employee', string='Shift In-charge')
    line_ids = fields.One2many('pump.shift.sheet.line', 'sheet_id', string='Nozzle Readings', copy=False)
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    total_qty = fields.Float(compute='_compute_totals', store=True, digits=(16, 2), string='Total Litres')
    total_amount = fields.Monetary(compute='_compute_totals', store=True, string='Total Fuel Sale')
    credit_amount = fields.Monetary(compute='_compute_totals', store=True, string='Credit Slips',
                                    help='Confirmed credit filling slips of this date & shift.')
    card_amount = fields.Monetary('Card (POS) Sale')
    upi_amount = fields.Monetary('UPI / Wallet Sale')
    fleet_card_amount = fields.Monetary('Fleet Card / SmartFleet')
    expected_cash = fields.Monetary(compute='_compute_totals', store=True)
    cash_collected = fields.Monetary('Cash Collected', help='Actual cash handed over by attendants.')
    short_excess = fields.Monetary(compute='_compute_totals', store=True, string='Short (-) / Excess (+)')
    voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    note = fields.Text()
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted'), ('cancel', 'Cancelled')],
                             default='draft', tracking=True, copy=False)

    _date_shift_uniq = models.Constraint('UNIQUE(date, shift_id)', 'A sheet already exists for this date and shift.')

    @api.depends('line_ids.sale_qty', 'line_ids.amount', 'card_amount', 'upi_amount', 'fleet_card_amount',
                 'cash_collected', 'date', 'shift_id')
    def _compute_totals(self):
        Sale = self.env['pump.credit.sale']
        for s in self:
            s.total_qty = sum(s.line_ids.mapped('sale_qty'))
            s.total_amount = sum(s.line_ids.mapped('amount'))
            credit = 0.0
            if s.date and s.shift_id:
                credit = sum(Sale.search([('date', '=', s.date), ('shift_id', '=', s.shift_id.id),
                                          ('source', '=', 'nozzle'),
                                          ('state', 'in', ('confirmed', 'billed'))]).mapped('amount'))
            s.credit_amount = credit
            s.expected_cash = s.total_amount - credit - s.card_amount - s.upi_amount - s.fleet_card_amount
            s.short_excess = (s.cash_collected - s.expected_cash) if s.cash_collected else 0.0

    def action_load_nozzles(self):
        self.env['pump.nozzle']._invalidate_readings()
        for s in self.filtered(lambda x: x.state == 'draft'):
            existing = s.line_ids.mapped('nozzle_id')
            nozzles = self.env['pump.nozzle'].search([('state', '=', 'running')]) - existing
            s.write({'line_ids': [(0, 0, {'nozzle_id': n.id, 'opening': n.last_reading,
                                          'closing': n.last_reading,
                                          'rate': n.product_id.get_rate(s.date)}) for n in nozzles]})
        return True

    @api.model_create_multi
    def create(self, vals_list):
        sheets = super().create(vals_list)
        for s in sheets:
            if not s.line_ids:
                s.action_load_nozzles()
        return sheets

    def action_recompute(self):
        self._compute_totals()

    def action_post(self):
        Move = self.env['pump.stock.move']
        self.env.flush_all()
        self.env['pump.nozzle']._invalidate_readings()
        acc = lambda x: self.env.ref('zivoit_fuel_management.%s' % x)
        for s in self.filtered(lambda x: x.state == 'draft'):
            s._compute_totals()
            if not s.line_ids:
                raise UserError(self.env._('Add nozzle readings before posting.'))
            prev_open = self.search([('state', '=', 'draft'), ('id', '!=', s.id), '|',
                                     ('date', '<', s.date), '&', ('date', '=', s.date),
                                     ('shift_sequence', '<', s.shift_sequence)], limit=1)
            if prev_open:
                raise UserError(self.env._('Post earlier sheet %s first so meter readings stay in sequence.', prev_open.name))
            for line in s.line_ids:
                if line.closing < line.opening:
                    raise UserError(self.env._('Closing reading is lower than opening on nozzle %s.', line.nozzle_id.name))
                if abs(line.opening - line.nozzle_id.last_reading) > 0.01:
                    raise UserError(self.env._('Opening reading of %(n)s (%(o).2f) does not match last closing (%(l).2f).',
                                      n=line.nozzle_id.name, o=line.opening, l=line.nozzle_id.last_reading))
            if s.name in (False, '/'):
                s.name = self.env['ir.sequence'].next_by_code('pump.shift.sheet') or '/'
            label = self.env._('Shift sale %(d)s %(s)s', d=s.date.strftime('%d-%m-%Y'), s=s.shift_id.name)
            lines = []
            by_product = {}
            for line in s.line_ids:
                if line.sale_qty:
                    Move.create({'date': s.date, 'tank_id': line.nozzle_id.tank_id.id, 'qty': -line.sale_qty,
                                 'move_type': 'sale', 'reference': '%s / %s' % (s.name, line.nozzle_id.name),
                                 'res_model': s._name, 'res_id': s.id})
                by_product.setdefault(line.product_id, 0.0)
                by_product[line.product_id] += line.amount
            for product, amount in by_product.items():
                lines.append({'account_id': product.income_account_id.id, 'credit': amount,
                              'name': '%s - %s' % (label, product.name)})
            lines += [
                {'account_id': acc('acc_credit_clearing').id, 'debit': s.credit_amount, 'name': self.env._('Credit slips')},
                {'account_id': acc('acc_card').id, 'debit': s.card_amount, 'name': self.env._('Card sale')},
                {'account_id': acc('acc_upi').id, 'debit': s.upi_amount, 'name': self.env._('UPI sale')},
                {'account_id': acc('acc_fleet').id, 'debit': s.fleet_card_amount, 'name': self.env._('Fleet card sale')},
            ]
            if s.cash_collected:
                lines.append({'account_id': acc('acc_cash').id, 'debit': s.cash_collected, 'name': self.env._('Cash collected')})
                diff = s.cash_collected - s.expected_cash
                lines.append({'account_id': acc('acc_short_excess').id, 'debit': -diff if diff < 0 else 0.0,
                              'credit': diff if diff > 0 else 0.0, 'name': self.env._('Cash short / excess')})
            else:
                lines.append({'account_id': acc('acc_cash').id, 'debit': s.expected_cash, 'name': self.env._('Cash sale')})
            if any(l.get('debit', 0) < -0.005 for l in lines if l['account_id'] == acc('acc_cash').id):
                raise UserError(self.env._('Non-cash collections exceed the total sale. Check card/UPI/credit amounts.'))
            voucher = self.env['pump.voucher']._create_posted(
                lines, voucher_type='shift', date=s.date, shift_id=s.shift_id.id, ref=s.name,
                narration=label, origin_model=s._name, origin_id=s.id)
            s.write({'voucher_id': voucher.id, 'state': 'posted'})
            self.env.flush_all()
            self.env['pump.nozzle']._invalidate_readings()
        return True

    def action_cancel(self):
        for s in self:
            later = self.search([('state', '=', 'posted'), ('id', '!=', s.id), '|', ('date', '>', s.date),
                                 '&', ('date', '=', s.date), ('shift_sequence', '>', s.shift_sequence)], limit=1)
            if s.state == 'posted' and later:
                raise UserError(self.env._('Cancel later sheet %s first.', later.name))
            if s.voucher_id:
                s.voucher_id.with_context(pump_from_origin=True).action_cancel()
            self.env['pump.stock.move'].search([('res_model', '=', s._name), ('res_id', '=', s.id)]).unlink()
            s.state = 'cancel'
        self.env['pump.nozzle']._invalidate_readings()
        return True

    def action_draft(self):
        self.filtered(lambda s: s.state == 'cancel').write({'state': 'draft', 'voucher_id': False})

    def action_print(self):
        return self.env.ref('zivoit_fuel_management.action_report_shift').report_action(self)

    def product_summary(self):
        self.ensure_one()
        res = {}
        for l in self.line_ids:
            r = res.setdefault(l.product_id, {'product': l.product_id, 'qty': 0.0, 'testing': 0.0, 'amount': 0.0})
            r['qty'] += l.sale_qty
            r['testing'] += l.testing_qty
            r['amount'] += l.amount
        return list(res.values())


class PumpShiftSheetLine(models.Model):
    _name = 'pump.shift.sheet.line'
    _description = 'Nozzle Reading'
    _order = 'sheet_id, nozzle_sequence, id'

    sheet_id = fields.Many2one('pump.shift.sheet', required=True, ondelete='cascade')
    date = fields.Date(related='sheet_id.date', store=True)
    shift_sequence = fields.Integer(related='sheet_id.shift_sequence', store=True)
    state = fields.Selection(related='sheet_id.state', store=True)
    nozzle_id = fields.Many2one('pump.nozzle', required=True)
    nozzle_sequence = fields.Integer(related='nozzle_id.sequence', store=True, string='Nozzle Sequence')
    product_id = fields.Many2one(related='nozzle_id.product_id', store=True)
    employee_id = fields.Many2one('hr.employee', string='Attendant (DSM)')
    opening = fields.Float('Opening', digits=(16, 2))
    closing = fields.Float('Closing', digits=(16, 2))
    testing_qty = fields.Float('Testing', digits=(16, 2), help='Litres returned to tank after 5-litre test.')
    gross_qty = fields.Float(compute='_compute_amount', store=True, digits=(16, 2), string='Meter Qty')
    sale_qty = fields.Float(compute='_compute_amount', store=True, digits=(16, 2), string='Sale Qty')
    rate = fields.Float(digits=(16, 2))
    currency_id = fields.Many2one(related='sheet_id.currency_id')
    amount = fields.Monetary(compute='_compute_amount', store=True)

    @api.depends('opening', 'closing', 'testing_qty', 'rate')
    def _compute_amount(self):
        for l in self:
            l.gross_qty = max(l.closing - l.opening, 0.0)
            l.sale_qty = max(l.gross_qty - l.testing_qty, 0.0)
            l.amount = round(l.sale_qty * l.rate, 2)

    @api.onchange('nozzle_id')
    def _onchange_nozzle(self):
        if self.nozzle_id:
            self.opening = self.nozzle_id.last_reading
            self.closing = self.closing or self.opening
            self.rate = self.nozzle_id.product_id.get_rate(self.sheet_id.date or fields.Date.context_today(self))

    @api.constrains('opening', 'closing', 'testing_qty')
    def _check_reading(self):
        for l in self:
            if l.closing < l.opening:
                raise ValidationError(self.env._('Closing reading (%(c).2f) cannot be below opening (%(o).2f) on %(n)s.',
                                        c=l.closing, o=l.opening, n=l.nozzle_id.name))
            if l.testing_qty < 0 or l.testing_qty > (l.closing - l.opening) + 0.001:
                raise ValidationError(self.env._('Testing quantity must be between 0 and the metered quantity.'))
