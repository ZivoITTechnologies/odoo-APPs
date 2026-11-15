# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class PumpTank(models.Model):
    _name = 'pump.tank'
    _description = 'Underground Storage Tank'
    _order = 'name'

    name = fields.Char(required=True)
    product_id = fields.Many2one('pump.product', required=True, domain=[('product_type', '=', 'fuel')])
    capacity = fields.Float('Capacity (Ltr)', required=True, digits=(16, 0))
    dead_stock = fields.Float('Dead Stock (Ltr)', digits=(16, 0), default=0)
    reorder_level = fields.Float('Re-order Level (Ltr)', digits=(16, 0))
    active = fields.Boolean(default=True)
    opening_stock = fields.Float('Opening Stock (Ltr)', digits=(16, 2))
    opening_date = fields.Date('Opening Stock Date')
    nozzle_ids = fields.One2many('pump.nozzle', 'tank_id')
    dip_chart_ids = fields.One2many('pump.dip.chart', 'tank_id', string='DIP Chart')
    stock_qty = fields.Float('Book Stock (Ltr)', compute='_compute_stock', digits=(16, 2))
    fill_percent = fields.Float(compute='_compute_stock', digits=(16, 1))
    ullage = fields.Float('Ullage (Ltr)', compute='_compute_stock', digits=(16, 0),
                          help='Free space available for the next tanker.')

    def _compute_stock(self):
        data = self.env['pump.stock.move']._read_group(
            [('tank_id', 'in', self.ids)], ['tank_id'], ['qty:sum'])
        res = {t.id: q for t, q in data}
        for t in self:
            qty = res.get(t.id, 0.0)
            t.stock_qty = qty
            t.fill_percent = (qty / t.capacity * 100.0) if t.capacity else 0.0
            t.ullage = max(t.capacity - qty, 0.0)

    def action_post_opening(self):
        """Book the opening stock of a tank (once) from the DIP at go-live."""
        Move = self.env['pump.stock.move']
        for t in self:
            if Move.search_count([('tank_id', '=', t.id), ('move_type', '=', 'opening')]):
                raise UserError(_('Opening stock already posted for %s.', t.name))
            if t.opening_stock <= 0:
                raise UserError(_('Enter the opening stock first.'))
            Move.create({'date': t.opening_date or fields.Date.context_today(self), 'tank_id': t.id,
                         'qty': t.opening_stock, 'move_type': 'opening', 'reference': _('Opening stock')})
        return True

    def volume_from_dip(self, dip_mm):
        """Linear interpolation on the tank's DIP (calibration) chart."""
        self.ensure_one()
        chart = self.dip_chart_ids.sorted('dip_mm')
        if not chart:
            raise UserError(_('No DIP chart defined for tank %s.', self.name))
        if dip_mm <= chart[0].dip_mm:
            return chart[0].volume * (dip_mm / chart[0].dip_mm) if chart[0].dip_mm else chart[0].volume
        for lo, hi in zip(chart, chart[1:]):
            if lo.dip_mm <= dip_mm <= hi.dip_mm:
                span = hi.dip_mm - lo.dip_mm
                return lo.volume + (hi.volume - lo.volume) * ((dip_mm - lo.dip_mm) / span if span else 0)
        return chart[-1].volume

    def action_stock_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Stock Ledger: %s', self.name),
            'res_model': 'pump.stock.move', 'view_mode': 'list,pivot',
            'domain': [('tank_id', '=', self.id)],
        }


class PumpDipChart(models.Model):
    _name = 'pump.dip.chart'
    _description = 'DIP Calibration Chart'
    _order = 'tank_id, dip_mm'

    tank_id = fields.Many2one('pump.tank', required=True, ondelete='cascade')
    dip_mm = fields.Float('DIP (mm)', required=True)
    volume = fields.Float('Volume (Ltr)', required=True)


class PumpNozzle(models.Model):
    _name = 'pump.nozzle'
    _description = 'Dispenser Nozzle'
    _order = 'sequence, name'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    dispenser = fields.Char('Dispensing Unit (MPD)')
    tank_id = fields.Many2one('pump.tank', required=True)
    product_id = fields.Many2one(related='tank_id.product_id', store=True)
    opening_meter = fields.Float('Initial Meter Reading', digits=(16, 2),
                                 help='Totaliser reading at go-live (or after meter reset).')
    state = fields.Selection([('running', 'Running'), ('stopped', 'Stopped')], default='running', required=True)
    stop_reason = fields.Char()
    active = fields.Boolean(default=True)
    last_reading = fields.Float(compute='_compute_last_reading', digits=(16, 2))
    last_reading_date = fields.Date(compute='_compute_last_reading')

    def _compute_last_reading(self):
        Line = self.env['pump.shift.sheet.line']
        for n in self:
            line = Line.search([('nozzle_id', '=', n.id), ('sheet_id.state', '=', 'posted')],
                               order='date desc, shift_sequence desc, id desc', limit=1)
            n.last_reading = line.closing if line else n.opening_meter
            n.last_reading_date = line.date if line else False

    @api.model
    def _invalidate_readings(self):
        self.invalidate_model(['last_reading', 'last_reading_date'])

    def action_toggle_state(self):
        for n in self:
            n.state = 'stopped' if n.state == 'running' else 'running'


class PumpDipReading(models.Model):
    _name = 'pump.dip.reading'
    _description = 'DIP Reading / Physical Stock'
    _order = 'date desc, id desc'

    date = fields.Date(required=True, default=fields.Date.context_today)
    tank_id = fields.Many2one('pump.tank', required=True)
    product_id = fields.Many2one(related='tank_id.product_id', store=True)
    dip_mm = fields.Float('DIP (mm)')
    physical_qty = fields.Float('Physical Stock (Ltr)', digits=(16, 2), required=True)
    book_qty = fields.Float('Book Stock (Ltr)', digits=(16, 2), readonly=True)
    variance = fields.Float(compute='_compute_variance', store=True, digits=(16, 2),
                            help='Physical minus book. Negative = shortage (evaporation/loss).')
    water_mm = fields.Float('Water DIP (mm)')
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted')], default='draft')
    note = fields.Char()

    @api.depends('physical_qty', 'book_qty')
    def _compute_variance(self):
        for r in self:
            r.variance = r.physical_qty - r.book_qty

    @api.onchange('tank_id', 'dip_mm')
    def _onchange_dip(self):
        if self.tank_id:
            self.book_qty = self.tank_id.stock_qty
            if self.dip_mm and self.tank_id.dip_chart_ids:
                self.physical_qty = round(self.tank_id.volume_from_dip(self.dip_mm), 2)

    def action_post(self):
        for r in self.filtered(lambda x: x.state == 'draft'):
            r.book_qty = r.tank_id.stock_qty
            r._compute_variance()
            if round(r.variance, 2):
                self.env['pump.stock.move'].create({
                    'date': r.date, 'tank_id': r.tank_id.id, 'qty': r.variance,
                    'move_type': 'gain' if r.variance > 0 else 'loss',
                    'reference': _('DIP %s', r.date), 'res_model': r._name, 'res_id': r.id,
                })
            r.state = 'posted'


class PumpStockMove(models.Model):
    _name = 'pump.stock.move'
    _description = 'Tank Stock Movement'
    _order = 'date desc, id desc'

    date = fields.Date(required=True, index=True)
    tank_id = fields.Many2one('pump.tank', required=True, index=True)
    product_id = fields.Many2one(related='tank_id.product_id', store=True, index=True)
    qty = fields.Float('Qty (Ltr)', digits=(16, 2), help='Positive = receipt, negative = issue.')
    qty_in = fields.Float(compute='_compute_in_out', store=True, digits=(16, 2), string='Receipt')
    qty_out = fields.Float(compute='_compute_in_out', store=True, digits=(16, 2), string='Issue')
    move_type = fields.Selection([
        ('opening', 'Opening Stock'), ('purchase', 'Tanker Receipt'), ('sale', 'Nozzle Sale'),
        ('gain', 'DIP Gain'), ('loss', 'DIP Loss'), ('adjust', 'Manual Adjustment'),
        ('internal', 'Own Fleet Issue'), ('delivery', 'Doorstep Delivery')], required=True)
    reference = fields.Char()
    res_model = fields.Char()
    res_id = fields.Integer()

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        self._invalidate_stock()
        moves._check_capacity()
        return moves

    def unlink(self):
        res = super().unlink()
        self._invalidate_stock()
        return res

    def _invalidate_stock(self):
        self.env['pump.tank'].invalidate_model(['stock_qty', 'fill_percent', 'ullage'])
        self.env['pump.product'].invalidate_model(['stock_qty'])

    @api.depends('qty')
    def _compute_in_out(self):
        for m in self:
            m.qty_in = m.qty if m.qty > 0 else 0.0
            m.qty_out = -m.qty if m.qty < 0 else 0.0

    def _check_capacity(self):
        for m in self:
            if m.qty > 0 and m.tank_id.capacity and m.tank_id.stock_qty > m.tank_id.capacity * 1.02:
                raise ValidationError(_('Tank %(t)s would exceed its capacity of %(c).0f Ltr.',
                                        t=m.tank_id.name, c=m.tank_id.capacity))
