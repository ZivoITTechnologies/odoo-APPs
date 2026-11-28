# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ZivoExpenseClaimWizard(models.TransientModel):
    _name = 'zivo.expense.claim.wizard'
    _description = 'Create Field Expense Claims'

    date_from = fields.Date(required=True, default=lambda self: self.env['zivo.expense.claim']._default_period()[0])
    date_to = fields.Date(required=True, default=lambda self: self.env['zivo.expense.claim']._default_period()[1])
    user_ids = fields.Many2many(
        'res.users', string='Salespeople', domain=[('zivo_tracking_enabled', '=', True)],
        default=lambda self: self.env.user if self.env.user.zivo_tracking_enabled else False)
    is_manager = fields.Boolean(default=lambda self: self.env.user.has_group(
        'zivoit_field_tracking.group_field_manager'))
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    @api.onchange('date_from')
    def _onchange_date_from(self):
        if self.date_from:
            self.date_to = self.env['zivo.expense.claim']._default_period(self.date_from)[1]

    def action_create(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_('The start date must be before the end date.'))
        users = self.user_ids or self.env.user
        if not self.env.user.has_group('zivoit_field_tracking.group_field_manager') and users != self.env.user:
            raise UserError(_('You can only create your own claims.'))
        Claim = self.env['zivo.expense.claim']
        claims = Claim.browse()
        for user in users:
            overlapping = Claim.sudo().search([
                ('user_id', '=', user.id), ('company_id', '=', self.company_id.id), ('state', '=', 'draft'),
                ('date_from', '<=', self.date_to), ('date_to', '>=', self.date_from)], limit=1)
            if overlapping:
                overlapping._fill_from_tracking()
                claims |= overlapping
                continue
            claims |= Claim._create_for(user, self.date_from, self.date_to, self.company_id)
        action = {
            'type': 'ir.actions.act_window', 'name': _('Expense Claims'), 'res_model': 'zivo.expense.claim',
            'domain': [('id', 'in', claims.ids)],
        }
        if len(claims) == 1:
            action.update({'view_mode': 'form', 'res_id': claims.id})
        else:
            action['view_mode'] = 'list,form'
        return action
