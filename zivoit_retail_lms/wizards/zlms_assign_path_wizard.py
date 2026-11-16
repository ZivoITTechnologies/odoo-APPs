# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class ZlmsAssignPathWizard(models.TransientModel):
    _name = 'zlms.assign.path.wizard'
    _description = 'Assign Learning Path'

    path_id = fields.Many2one('zlms.learning.path', required=True, domain=[('state', '=', 'published')])
    target = fields.Selection([('learners', 'Selected Learners'), ('stores', 'Whole Stores'),
                               ('regions', 'Regions'), ('roles', 'Roles (all stores)')],
                              default='stores', required=True)
    learner_ids = fields.Many2many('zlms.learner')
    store_ids = fields.Many2many('zlms.store')
    region_ids = fields.Many2many('zlms.region')
    role_ids = fields.Many2many('zlms.role')
    due_date = fields.Date(help='Leave empty to use the path duration.')

    def _get_learners(self):
        Learner = self.env['zlms.learner']
        if self.target == 'learners':
            learners = self.learner_ids
        elif self.target == 'stores':
            learners = Learner.search([('store_id', 'in', self.store_ids.ids)])
        elif self.target == 'regions':
            learners = Learner.search([('region_id', 'in', self.region_ids.ids)])
        else:
            learners = Learner.search([('role_id', 'in', self.role_ids.ids)])
        if self.target != 'roles' and self.role_ids:
            learners = learners.filtered(lambda l: l.role_id in self.role_ids)
        return learners.filtered('active')

    def action_assign(self):
        self.ensure_one()
        if self.path_id.state != 'published':
            raise UserError(_('Publish the learning path before assigning it.'))
        learners = self._get_learners()
        if not learners:
            raise UserError(_('No learners match this selection.'))
        assignments = self.path_id._assign_to_learners(learners, due_date=self.due_date)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Learning path assigned'),
                       'message': _('%(new)s new assignment(s) created for %(total)s learner(s).',
                                    new=len(assignments), total=len(learners)),
                       'type': 'success', 'next': {'type': 'ir.actions.act_window_close'}},
        }
