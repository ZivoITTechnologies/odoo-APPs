# -*- coding: utf-8 -*-
"""AI Learning Path Builder: designs a journey from existing content plus on-the-job tasks."""
from odoo import _, api, fields, models

from ..models.ai_service import ZlmsAiUnavailable
from ..models.learning_path import PATH_TYPES

# path type -> (course categories, role-play skill areas, default OJT tasks)
BLUEPRINTS = {
    'onboarding': (['onboarding', 'service', 'product', 'compliance'], ['discovery', 'greeting'],
                   ['Greets every customer within 10 seconds with eye contact',
                    'Completes a sale end-to-end following the billing SOP unassisted',
                    'Explains the top 5 products using feature-advantage-benefit']),
    'product': (['product'], ['demo'], ['Demonstrates three key products to a customer with benefits']),
    'service': (['service', 'onboarding'], ['complaint', 'greeting', 'return'],
                ['Handles a customer concern using LEARN without escalation']),
    'selling': (['selling', 'service', 'product'], ['price_objection', 'cross_sell', 'quality_objection'],
                ['Makes two relevant add-on suggestions in one shift', 'Converts a price objection with a value story']),
    'vm': (['vm'], [], ['Sets a display to planogram standard', 'Completes the opening VM checklist']),
    'compliance': (['compliance'], [], ['Completes the store opening and closing checklist correctly']),
    'leadership': (['leadership', 'compliance'], ['complaint'],
                   ['Runs a 10-minute morning huddle', 'Conducts two coaching observations']),
    'launch': (['product'], ['demo'], ['Pitches the new collection to three customers']),
    'refresher': (['service', 'product', 'compliance'], [], ['Re-demonstrates core service standards']),
}


class ZlmsAiPathWizard(models.TransientModel):
    _name = 'zlms.ai.path.wizard'
    _description = 'AI Learning Path Builder'

    name = fields.Char(string='Journey Name', required=True)
    path_type = fields.Selection(PATH_TYPES, required=True, default='onboarding')
    role_ids = fields.Many2many('zlms.role', string='Target Roles')
    duration_days = fields.Integer(default=30)
    goal = fields.Text(string='Business Goal',
                       help='e.g. "New associates should sell confidently during the festive season peak."')
    include_roleplay = fields.Boolean(default=True)
    include_ojt = fields.Boolean(string='Include on-the-job tasks', default=True)
    include_sessions = fields.Boolean(string='Include classroom sessions', default=True)
    auto_assign = fields.Boolean(default=False)
    use_ai = fields.Boolean(default=lambda self: self.env['zlms.ai.service'].is_enabled())
    ai_available = fields.Boolean(default=lambda self: self.env['zlms.ai.service'].is_enabled())

    def _catalogue(self):
        channels = self.env['slide.channel'].search([('is_published', '=', True)])
        scenarios = self.env['zlms.roleplay.scenario'].search([]) if self.include_roleplay else \
            self.env['zlms.roleplay.scenario']
        ilt = self.env['zlms.ilt.course'].search([]) if self.include_sessions else self.env['zlms.ilt.course']
        return channels, scenarios, ilt

    def action_build(self):
        self.ensure_one()
        steps, engine = None, 'offline'
        if self.use_ai and self.env['zlms.ai.service'].is_enabled():
            try:
                steps = self._build_ai()
                engine = 'ai'
            except ZlmsAiUnavailable:
                steps = None
        if not steps:
            steps = self._build_offline()
        path = self.env['zlms.learning.path'].create({
            'name': self.name, 'path_type': self.path_type, 'duration_days': self.duration_days,
            'role_ids': [(6, 0, self.role_ids.ids)], 'auto_assign': self.auto_assign,
            'description': self.goal and '<p>%s</p>' % self.goal,
            'line_ids': [(0, 0, dict(step, sequence=i + 1)) for i, step in enumerate(steps)],
        })
        path.message_post(body=_('Journey designed by the %s engine. Review the steps, then publish.',
                                 _('AI') if engine == 'ai' else _('offline')))
        return {'type': 'ir.actions.act_window', 'res_model': 'zlms.learning.path', 'res_id': path.id,
                'view_mode': 'form', 'target': 'current'}

    def _spread(self, n, index):
        return max(1, round((index + 1) * (self.duration_days or 30) / (n + 1)))

    def _build_offline(self):
        channels, scenarios, ilt = self._catalogue()
        cats, areas, ojt = BLUEPRINTS.get(self.path_type, BLUEPRINTS['refresher'])
        picked = []
        for cat in cats:
            for channel in channels.filtered(lambda c, c_=cat: c.zlms_retail_category == c_)[:2]:
                picked.append({'name': channel.name, 'line_type': 'course', 'channel_id': channel.id})
        for area in areas:
            for scenario in scenarios.filtered(lambda s, a=area: s.skill_area == a)[:1]:
                picked.append({'name': _('AI practice: %s', scenario.name), 'line_type': 'roleplay',
                               'scenario_id': scenario.id, 'min_score': 60})
        if ilt:
            picked.append({'name': ilt[0].name, 'line_type': 'session', 'ilt_course_id': ilt[0].id})
        if self.include_ojt:
            picked += [{'name': _('OJT: %s', task), 'line_type': 'ojt', 'instructions': task} for task in ojt]
        if not picked:
            picked = [{'name': _('OJT: %s', task), 'line_type': 'ojt', 'instructions': task} for task in ojt]
        for i, step in enumerate(picked):
            step['day_offset'] = self._spread(len(picked), i)
        return picked

    def _build_ai(self):
        channels, scenarios, ilt = self._catalogue()
        catalogue = {
            'courses': [{'id': c.id, 'name': c.name, 'category': c.zlms_retail_category or ''} for c in channels],
            'roleplays': [{'id': s.id, 'name': s.name, 'skill': s.skill_area} for s in scenarios],
            'sessions': [{'id': c.id, 'name': c.name} for c in ilt],
        }
        prompt = (
            'Design a %(days)s-day retail learning journey "%(name)s" (type: %(type)s) for roles: %(roles)s.\n'
            'Business goal: %(goal)s\nUse ONLY items from this catalogue (by id):\n%(cat)s\n'
            '%(ojt)s Order steps from foundation to application and spread due days across the journey.\n'
            'Return JSON: {"steps": [{"type": "course|roleplay|session|ojt", "id": int (not for ojt), '
            '"name": str, "day": int, "instructions": str (ojt only)}]}'
        ) % {'days': self.duration_days, 'name': self.name, 'type': self.path_type,
             'roles': ', '.join(self.role_ids.mapped('name')) or 'all store roles', 'goal': self.goal or '-',
             'cat': catalogue, 'ojt': 'Add 1-3 observable on-the-job tasks (type ojt).' if self.include_ojt else ''}
        data = self.env['zlms.ai.service'].chat_json(
            prompt, system=self.env['zlms.ai.service'].brand_context() + ' You are a retail L&D architect.',
            feature='path_builder')
        valid = {'course': set(channels.ids), 'roleplay': set(scenarios.ids), 'session': set(ilt.ids)}
        field = {'course': 'channel_id', 'roleplay': 'scenario_id', 'session': 'ilt_course_id'}
        steps = []
        for raw in data.get('steps') or []:
            stype = raw.get('type')
            day = int(raw.get('day') or 1)
            if stype == 'ojt' and self.include_ojt:
                steps.append({'name': str(raw.get('name') or 'On-the-job task')[:120], 'line_type': 'ojt',
                              'instructions': str(raw.get('instructions') or ''), 'day_offset': day})
            elif stype in valid and int(raw.get('id') or 0) in valid[stype]:
                step = {'name': str(raw.get('name') or '')[:120] or stype, 'line_type': stype,
                        field[stype]: int(raw['id']), 'day_offset': day}
                if stype == 'roleplay':
                    step['min_score'] = 60
                steps.append(step)
        if not steps:
            raise ZlmsAiUnavailable('AI returned no usable steps')
        return steps
