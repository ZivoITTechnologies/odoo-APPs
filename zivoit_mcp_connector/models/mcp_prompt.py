# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
import re

from odoo import fields, models

PLACEHOLDER = re.compile(r'\{(\w+)\}')


class ZivoMcpPrompt(models.Model):
    _name = 'zivo.mcp.prompt'
    _description = 'AI Prompt Library'
    _order = 'sequence, id'

    name = fields.Char(string="Title", required=True, translate=True)
    technical_name = fields.Char(required=True, help="Prompt name sent to the AI client (prompts/list).")
    category = fields.Selection([
        ('finance', 'Finance'), ('sales', 'Sales'), ('purchase', 'Purchase'),
        ('inventory', 'Inventory'), ('management', 'Management'), ('other', 'Other'),
    ], default='finance', required=True)
    description = fields.Char(translate=True)
    template = fields.Text(required=True, translate=True,
                           help="Text of the prompt. Use {argument} placeholders for the arguments below.")
    argument_ids = fields.One2many('zivo.mcp.prompt.argument', 'prompt_id', string="Arguments", copy=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _technical_name_uniq = models.Constraint('unique(technical_name)', 'Prompt names must be unique.')

    def _mcp_definition(self):
        self.ensure_one()
        return {
            'name': self.technical_name,
            'title': self.name,
            'description': self.description or self.name,
            'arguments': [{'name': a.name, 'description': a.description or a.name, 'required': a.required}
                          for a in self.argument_ids],
        }

    def _mcp_render(self, arguments):
        self.ensure_one()
        missing = [a.name for a in self.argument_ids if a.required and not arguments.get(a.name)]
        if missing:
            raise ValueError('Missing required argument(s): %s' % ', '.join(missing))
        defaults = {a.name: a.default_value or '' for a in self.argument_ids}

        def replace(match):
            key = match.group(1)
            value = arguments.get(key) or defaults.get(key)
            return str(value) if value not in (None, '') else match.group(0) if key not in defaults else ''

        text = PLACEHOLDER.sub(replace, self.template)
        return {
            'description': self.description or self.name,
            'messages': [{'role': 'user', 'content': {'type': 'text', 'text': text.strip()}}],
        }


class ZivoMcpPromptArgument(models.Model):
    _name = 'zivo.mcp.prompt.argument'
    _description = 'AI Prompt Argument'
    _order = 'sequence, id'

    prompt_id = fields.Many2one('zivo.mcp.prompt', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    description = fields.Char(translate=True)
    required = fields.Boolean()
    default_value = fields.Char(string="Default")
