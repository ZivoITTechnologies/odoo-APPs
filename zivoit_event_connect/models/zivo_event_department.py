# -*- coding: utf-8 -*-
from odoo import api, fields, models



class ZivoEventDepartment(models.Model):
    _name = 'zivo.event.department'
    _description = 'Event Department'
    _order = 'sequence, name'
    _parent_store = True

    name = fields.Char(required=True, translate=True)
    code = fields.Char(help='Short code used in the API and website filters (e.g. "hr", "it").')
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    active = fields.Boolean(default=True)
    parent_id = fields.Many2one('zivo.event.department', string='Parent Department', index=True, ondelete='cascade')
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('zivo.event.department', 'parent_id', string='Sub-departments')
    manager_id = fields.Many2one('res.users', string='Department Manager')
    member_ids = fields.Many2many('res.users', 'zivo_event_department_user_rel', 'department_id', 'user_id',
                                  string='Members',
                                  help='Users of this department. Used for department-only events and '
                                       'equipment approval routing.')
    description = fields.Text()
    event_ids = fields.Many2many('event.event', 'zivo_event_department_rel', 'department_id', 'event_id', string='Events')
    event_count = fields.Integer(compute='_compute_event_count')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    _code_uniq = models.Constraint('unique(code, company_id)', 'Department code must be unique per company.')

    @api.depends('event_ids')
    def _compute_event_count(self):
        for dept in self:
            dept.event_count = len(dept.event_ids)

    @api.depends('name', 'parent_id.name', 'parent_id.parent_id.name')
    def _compute_display_name(self):
        for dept in self:
            names, node, guard = [], dept, 0
            while node and guard < 10:
                names.append(node.name or '')
                node, guard = node.parent_id, guard + 1
            dept.display_name = ' / '.join(reversed(names))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('code') and vals.get('name'):
                vals['code'] = self.env['ir.http']._slugify(vals['name'])
        return super().create(vals_list)

    def action_view_events(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('event.action_event_view')
        action['domain'] = [('zivo_department_ids', 'in', self.ids)]
        action['context'] = {'default_zivo_department_ids': [(6, 0, self.ids)]}
        return action
