# -*- coding: utf-8 -*-
from odoo import fields, models


class ZlmsRegion(models.Model):
    _name = 'zlms.region'
    _description = 'Retail Region / Zone'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    code = fields.Char()
    sequence = fields.Integer(default=10)
    manager_user_id = fields.Many2one('res.users', string='Area / Regional Manager')
    store_ids = fields.One2many('zlms.store', 'region_id', string='Stores')
    store_count = fields.Integer(compute='_compute_store_count')
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint('unique(code)', 'Region code must be unique.')

    def _compute_store_count(self):
        for region in self:
            region.store_count = len(region.store_ids)


class ZlmsRole(models.Model):
    _name = 'zlms.role'
    _description = 'Retail Job Role'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    is_manager_role = fields.Boolean(
        help='Learners with this role manage a store and can sign off on-the-job checklists.')
    description = fields.Text(translate=True)
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint('unique(code)', 'Role code must be unique.')


class ZlmsTechTag(models.Model):
    _name = 'zlms.tech.tag'
    _description = 'Product Feature / Technology Tag'
    _order = 'name'

    name = fields.Char(required=True, translate=True)
    description = fields.Text(translate=True, help='Plain-language explanation that store staff can repeat to customers.')
    color = fields.Integer()

    _name_uniq = models.Constraint('unique(name)', 'Technology tag already exists.')


class ZlmsSkill(models.Model):
    _name = 'zlms.skill'
    _description = 'Retail Competency / Skill'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    skill_area = fields.Selection([
        ('product', 'Product Knowledge'),
        ('service', 'Customer Service'),
        ('selling', 'Selling Skills'),
        ('operations', 'Store Operations'),
        ('vm', 'Visual Merchandising'),
        ('compliance', 'Compliance & Safety'),
        ('leadership', 'Leadership'),
    ], required=True, default='selling')
    description = fields.Text(translate=True)
