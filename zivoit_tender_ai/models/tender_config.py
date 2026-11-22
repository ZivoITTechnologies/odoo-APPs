# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .tender_utils import keyword_list


class TenderStage(models.Model):
    _name = 'zivoit.tender.stage'
    _description = 'Tender Pipeline Stage'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    fold = fields.Boolean(help='Folded in the kanban view')
    stage_type = fields.Selection([
        ('new', 'New / Found'),
        ('review', 'Under Review'),
        ('shortlist', 'Shortlisted'),
        ('prepare', 'Bid Preparation'),
        ('submitted', 'Submitted'),
        ('evaluation', 'Under Evaluation'),
        ('won', 'Won / Awarded'),
        ('lost', 'Lost'),
        ('dropped', 'Dropped / No-Bid'),
        ('expired', 'Expired / Missed'),
    ], required=True, default='new')
    is_closed = fields.Boolean(compute='_compute_is_closed', store=True)
    description = fields.Text(translate=True)
    mail_template_id = fields.Many2one(
        'mail.template', domain=[('model', '=', 'zivoit.tender')],
        help='E-mail sent to the tender followers when a tender reaches this stage.')

    @api.depends('stage_type')
    def _compute_is_closed(self):
        for stage in self:
            stage.is_closed = stage.stage_type in ('won', 'lost', 'dropped', 'expired')


class TenderSector(models.Model):
    _name = 'zivoit.tender.sector'
    _description = 'Construction Sector'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, help='Technical code used by the classifier and the AI agent')
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    keywords = fields.Text(
        help='Comma separated words or phrases. A tender containing any of them is classified in this sector.')
    exclude_keywords = fields.Text(help='Comma separated words that prevent classification in this sector.')
    active = fields.Boolean(default=True)
    tender_count = fields.Integer(compute='_compute_tender_count')

    _code_uniq = models.Constraint('UNIQUE(code)', 'The sector code must be unique.')

    def _compute_tender_count(self):
        data = self.env['zivoit.tender']._read_group(
            [('sector_ids', 'in', self.ids)], ['sector_ids'], ['__count'])
        counts = {sector.id: count for sector, count in data}
        for sector in self:
            sector.tender_count = counts.get(sector.id, 0)

    @api.model
    def _get_keyword_map(self):
        """{code: [keywords]} for all active sectors, used by the classifier."""
        result = {}
        for sector in self.search([]):
            result[sector.code] = keyword_list(sector.keywords)
        return result


class TenderTag(models.Model):
    _name = 'zivoit.tender.tag'
    _description = 'Tender Tag'
    _order = 'name'

    name = fields.Char(required=True, translate=True)
    color = fields.Integer()

    _name_uniq = models.Constraint('UNIQUE(name)', 'Tag name already exists.')


class TenderLostReason(models.Model):
    _name = 'zivoit.tender.lost.reason'
    _description = 'Tender Lost / Drop Reason'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    reason_type = fields.Selection([('lost', 'Lost'), ('dropped', 'No-Bid / Dropped'), ('both', 'Both')],
                                   default='both', required=True)
    active = fields.Boolean(default=True)


class TenderChecklistTemplate(models.Model):
    _name = 'zivoit.tender.checklist.template'
    _description = 'Bid / No-Bid Checklist Template'
    _order = 'sequence, name'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    sector_ids = fields.Many2many('zivoit.tender.sector', string='Applies to Sectors',
                                  help='Leave empty to apply to all sectors.')
    line_ids = fields.One2many('zivoit.tender.checklist.template.line', 'template_id', copy=True)
    is_default = fields.Boolean(string='Apply Automatically',
                                help='Applied to new tenders when they are shortlisted.')
    active = fields.Boolean(default=True)


class TenderChecklistTemplateLine(models.Model):
    _name = 'zivoit.tender.checklist.template.line'
    _description = 'Checklist Template Criterion'
    _order = 'sequence, id'

    template_id = fields.Many2one('zivoit.tender.checklist.template', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    name = fields.Char('Criterion', required=True)
    category = fields.Selection([
        ('eligibility', 'Eligibility / Pre-qualification'),
        ('technical', 'Technical Capability'),
        ('financial', 'Financial Capacity'),
        ('commercial', 'Commercial / Risk'),
        ('document', 'Document Required'),
        ('strategic', 'Strategic Fit'),
    ], required=True, default='eligibility')
    weight = fields.Integer(default=10)
    mandatory = fields.Boolean(help='If not met, the recommendation becomes No-Bid regardless of the score.')


class TenderZone(models.Model):
    _name = 'zivoit.tender.zone'
    _description = 'Railway Zone / PSU'
    _order = 'code'

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    zone_type = fields.Selection([('zone', 'Railway Zone'), ('psu', 'Railway PSU / Corporation')],
                                 default='zone', required=True)
    headquarters = fields.Char()
    state_id = fields.Many2one('res.country.state', string='HQ State')
    aliases = fields.Char(help='Comma separated names used in tender documents')
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint('UNIQUE(code)', 'Zone code must be unique.')

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for zone in self:
            zone.display_name = '%s - %s' % (zone.code, zone.name) if zone.code else zone.name
