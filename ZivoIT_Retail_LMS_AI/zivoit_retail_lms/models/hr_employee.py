# -*- coding: utf-8 -*-
from odoo import _, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    zlms_learner_ids = fields.One2many('zlms.learner', 'employee_id', string='Learner Profiles',
                                       groups='hr.group_hr_user')

    def action_zlms_create_learner(self):
        Learner = self.env['zlms.learner']
        learners = Learner
        for employee in self:
            learner = Learner.search([('employee_id', '=', employee.id)], limit=1)
            if not learner:
                learner = Learner.create({
                    'name': employee.name,
                    'employee_id': employee.id,
                    'email': employee.work_email,
                    'mobile': employee.mobile_phone,
                    'user_id': employee.user_id.id if employee.user_id and not Learner.search_count(
                        [('user_id', '=', employee.user_id.id)]) else False,
                    'employee_code': employee.barcode or False,
                })
            learners |= learner
        return {
            'type': 'ir.actions.act_window', 'name': _('Learner Profiles'), 'res_model': 'zlms.learner',
            'view_mode': 'list,form', 'domain': [('id', 'in', learners.ids)],
        }
