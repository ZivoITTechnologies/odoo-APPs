# -*- coding: utf-8 -*-
from odoo import fields, models


class SlideChannel(models.Model):
    _inherit = 'slide.channel'

    zlms_retail_category = fields.Selection([
        ('onboarding', 'Onboarding'),
        ('product', 'Product Knowledge'),
        ('service', 'Customer Service'),
        ('selling', 'Selling Skills'),
        ('vm', 'Visual Merchandising'),
        ('compliance', 'Compliance & SOP'),
        ('leadership', 'Leadership'),
    ], string='Retail Category')
    zlms_ai_generated = fields.Boolean(string='AI Generated', readonly=True)
    zlms_boost_enabled = fields.Boolean(string='Use in Daily Boost', default=True,
                                        help='Quiz questions of this course feed the spaced-repetition Daily Boost.')
    zlms_product_card_ids = fields.Many2many('zlms.product.card', string='Product Cards')


class SlideChannelPartner(models.Model):
    _inherit = 'slide.channel.partner'

    def _recompute_completion(self):
        res = super()._recompute_completion()
        self._zlms_sync_assignments()
        return res

    def _zlms_sync_assignments(self):
        if not self:
            return
        assignments = self.env['zlms.assignment'].sudo().search([
            ('partner_id', 'in', self.partner_id.ids),
            ('state', 'in', ('assigned', 'in_progress')),
            ('line_ids.channel_id', 'in', self.channel_id.ids),
        ])
        assignments._sync_progress()


class GamificationKarmaTracking(models.Model):
    _inherit = 'gamification.karma.tracking'

    def _get_origin_selection_values(self):
        return super()._get_origin_selection_values() + [('zlms.learner', self.env._('Retail Learner'))]
