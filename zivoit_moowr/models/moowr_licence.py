from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class MoowrLicence(models.Model):
    _name = 'moowr.licence'
    _description = 'MOOWR Licence'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'
    _check_company_auto = True

    name = fields.Char('Licence Number', required=True, tracking=True,
                       help="Private bonded warehouse licence number under Section 58 of the Customs Act, 1962.")
    permission_number = fields.Char('Section 65 Permission', tracking=True,
                                    help="Permission number to carry out manufacturing and other operations under Section 65.")
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    warehouse_id = fields.Many2one('stock.warehouse', 'Bonded Warehouse', required=True, check_company=True, tracking=True)
    location_ids = fields.One2many('stock.location', 'moowr_licence_id', 'Bonded Locations')
    responsible_id = fields.Many2one('res.users', 'Compliance Officer', default=lambda self: self.env.user, tracking=True)
    customs_station = fields.Char('Customs Station / Port Code')
    jurisdiction = fields.Char('Customs Jurisdiction')
    bond_officer = fields.Char('Bond Officer')
    bond_number = fields.Char('Bond Number (Section 59)')
    bond_amount = fields.Monetary('Bond Amount', currency_field='currency_id')
    date_start = fields.Date('Valid From', tracking=True)
    date_end = fields.Date('Valid Until', tracking=True)
    defer_igst = fields.Boolean('Defer IGST', default=True, tracking=True,
                                help="Keep IGST deferred until clearance. Untick if IGST becomes payable at import "
                                     "(for example once Section 65A is notified).")
    interest_rate = fields.Float('Interest Rate (% per year)', default=15.0,
                                 help="Interest rate applied to duty on goods cleared as such after the interest-free period.")
    interest_free_days = fields.Integer('Interest-free Days', default=90,
                                        help="Goods cleared as such after this many days attract interest under Section 61(2).")
    return_due_day = fields.Integer('Monthly Return Due Day', default=10,
                                    help="Day of the following month by which the monthly return must be filed.")
    require_boe = fields.Boolean('Require Bill of Entry on Bonded Receipts', default=True,
                                 help="Block validation of bonded receipts that are not covered by a confirmed bill of entry.")
    active = fields.Boolean(default=True)
    note = fields.Html('Notes')
    color = fields.Integer()

    validity_state = fields.Selection([
        ('valid', 'Valid'), ('expiring', 'Expiring Soon'), ('expired', 'Expired'), ('open', 'No End Date'),
    ], compute='_compute_validity_state', string='Validity')

    # KPIs
    duty_in_bond = fields.Monetary('Duty Deferred in Bond', compute='_compute_kpis', currency_field='currency_id')
    value_in_bond = fields.Monetary('Assessable Value in Bond', compute='_compute_kpis', currency_field='currency_id')
    open_boe_count = fields.Integer('Open Bills of Entry', compute='_compute_kpis')
    pending_clearance_count = fields.Integer('Clearances Pending', compute='_compute_kpis')
    pending_duty = fields.Monetary('Duty Pending Payment', compute='_compute_kpis', currency_field='currency_id')
    duty_paid_month = fields.Monetary('Duty Paid This Month', compute='_compute_kpis', currency_field='currency_id')
    aged_line_count = fields.Integer('Inputs Past Interest-free Period', compute='_compute_kpis')
    next_return_due = fields.Date('Next Return Due', compute='_compute_kpis')
    open_return_count = fields.Integer('Returns Not Submitted', compute='_compute_kpis')

    _name_company_uniq = models.Constraint(
        'UNIQUE(name, company_id)', 'A licence number must be unique per company.')

    @api.constrains('date_start', 'date_end')
    def _check_dates(self):
        for lic in self:
            if lic.date_start and lic.date_end and lic.date_end < lic.date_start:
                raise ValidationError(self.env._("The licence end date cannot be before its start date."))

    @api.constrains('interest_free_days', 'return_due_day', 'interest_rate')
    def _check_numbers(self):
        for lic in self:
            if lic.interest_free_days < 0 or lic.interest_rate < 0:
                raise ValidationError(self.env._("Interest settings cannot be negative."))
            if not 1 <= lic.return_due_day <= 28:
                raise ValidationError(self.env._("The return due day must be between 1 and 28."))

    @api.depends('date_end')
    def _compute_validity_state(self):
        today = fields.Date.context_today(self)
        for lic in self:
            if not lic.date_end:
                lic.validity_state = 'open'
            elif lic.date_end < today:
                lic.validity_state = 'expired'
            elif lic.date_end <= today + relativedelta(days=60):
                lic.validity_state = 'expiring'
            else:
                lic.validity_state = 'valid'

    def _compute_kpis(self):
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        Line = self.env['moowr.boe.line']
        Batch = self.env['moowr.batch']
        Clearance = self.env['moowr.clearance']
        Return = self.env['moowr.return']
        for lic in self:
            lines = Line.search([('licence_id', '=', lic.id), ('boe_state', '=', 'confirmed'), ('qty_balance', '>', 0)])
            batches = Batch.search([('licence_id', '=', lic.id), ('qty_balance', '>', 0)])
            lic.duty_in_bond = sum(lines.mapped('duty_balance')) + sum(batches.mapped('duty_balance'))
            lic.value_in_bond = sum(lines.mapped('value_balance')) + sum(batches.mapped('value_balance'))
            lic.open_boe_count = self.env['moowr.boe'].search_count([('licence_id', '=', lic.id), ('state', '=', 'confirmed')])
            pending = Clearance.search([('licence_id', '=', lic.id), ('state', 'in', ('draft', 'filed'))])
            lic.pending_clearance_count = len(pending)
            lic.pending_duty = sum(pending.mapped('amount_payable'))
            paid = Clearance.search([('licence_id', '=', lic.id), ('state', '=', 'paid'),
                                     ('payment_date', '>=', month_start)])
            lic.duty_paid_month = sum(paid.mapped('amount_payable'))
            lic.aged_line_count = len(lines.filtered(lambda l: l.age_days > lic.interest_free_days))
            open_returns = Return.search([('licence_id', '=', lic.id), ('state', '!=', 'submitted')], order='date_from')
            lic.open_return_count = len(open_returns)
            if open_returns:
                lic.next_return_due = open_returns[0].due_date
            else:
                lic.next_return_due = month_start + relativedelta(months=1, day=lic.return_due_day or 10)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def action_assign_warehouse_locations(self):
        """Mark the warehouse stock location (and children) as bonded for this licence."""
        for lic in self:
            if lic.warehouse_id.lot_stock_id and not lic.warehouse_id.lot_stock_id.moowr_licence_id:
                lic.warehouse_id.lot_stock_id.moowr_licence_id = lic

    @api.model_create_multi
    def create(self, vals_list):
        licences = super().create(vals_list)
        licences.action_assign_warehouse_locations()
        return licences

    def _action(self, xmlid, domain=None, context=None):
        action = self.env['ir.actions.act_window']._for_xml_id(xmlid)
        action['domain'] = domain or [('licence_id', 'in', self.ids)]
        ctx = {'default_licence_id': self[:1].id}
        ctx.update(context or {})
        action['context'] = ctx
        return action

    def action_open_boes(self):
        return self._action('zivoit_moowr.action_moowr_boe')

    def action_open_open_boes(self):
        return self._action('zivoit_moowr.action_moowr_boe', [('licence_id', 'in', self.ids), ('state', '=', 'confirmed')])

    def action_open_stock(self):
        return self._action('zivoit_moowr.action_moowr_boe_line',
                            [('licence_id', 'in', self.ids), ('boe_state', '=', 'confirmed'), ('qty_balance', '>', 0)])

    def action_open_aged(self):
        ids = self.env['moowr.boe.line'].search([('licence_id', 'in', self.ids), ('boe_state', '=', 'confirmed'),
                                                 ('qty_balance', '>', 0)]).filtered(
            lambda l: l.age_days > l.licence_id.interest_free_days).ids
        return self._action('zivoit_moowr.action_moowr_boe_line', [('id', 'in', ids)])

    def action_open_ledger(self):
        return self._action('zivoit_moowr.action_moowr_ledger')

    def action_open_clearances(self):
        return self._action('zivoit_moowr.action_moowr_clearance')

    def action_open_pending_clearances(self):
        return self._action('zivoit_moowr.action_moowr_clearance',
                            [('licence_id', 'in', self.ids), ('state', 'in', ('draft', 'filed'))])

    def action_open_returns(self):
        return self._action('zivoit_moowr.action_moowr_return')

    def action_open_batches(self):
        return self._action('zivoit_moowr.action_moowr_batch')

    def action_create_last_return(self):
        self.ensure_one()
        ret = self._ensure_return(fields.Date.context_today(self) - relativedelta(months=1))
        return {
            'type': 'ir.actions.act_window', 'res_model': 'moowr.return', 'res_id': ret.id,
            'view_mode': 'form', 'target': 'current',
        }

    def _ensure_return(self, any_day):
        self.ensure_one()
        date_from = any_day.replace(day=1)
        ret = self.env['moowr.return'].search([('licence_id', '=', self.id), ('date_from', '=', date_from)], limit=1)
        if not ret:
            ret = self.env['moowr.return'].create({'licence_id': self.id, 'date_from': date_from})
        return ret

    @api.model
    def _cron_moowr_reminders(self):
        today = fields.Date.context_today(self)
        activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for lic in self.search([]):
            ret = lic._ensure_return(today - relativedelta(months=1))
            user = lic.responsible_id or self.env.user
            if ret.state == 'draft' and activity_type and not ret.activity_ids:
                ret.activity_schedule(
                    activity_type_id=activity_type.id, user_id=user.id, date_deadline=ret.due_date,
                    summary=self.env._("File MOOWR monthly return %s", ret.name))
            if lic.validity_state == 'expiring' and activity_type and not lic.activity_ids:
                lic.activity_schedule(
                    activity_type_id=activity_type.id, user_id=user.id, date_deadline=lic.date_end,
                    summary=self.env._("MOOWR licence %s expires on %s", lic.name, lic.date_end))
