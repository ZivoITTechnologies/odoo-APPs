# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

CREATOR_FIELDS = {'state', 'vehicle_number', 'docket_number', 'eta', 'transporter_id', 'epod_state',
                  'actual_dispatch', 'actual_delivery'}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    zoho_vendor_id = fields.Char(string='Zoho Vendor ID', copy=False)


class CpFreightBill(models.Model):
    _inherit = 'cp.freight.bill'

    zoho_bill_id = fields.Char(string='Zoho Bill ID', copy=False, readonly=True)
    zoho_state = fields.Selection([('none', 'Not sent'), ('pending', 'Waiting'), ('done', 'In Zoho Books'),
                                   ('failed', 'Failed')], compute='_compute_zoho_state', string='Zoho Books')

    def _compute_zoho_state(self):
        Job = self.env['cp.zoho.sync']
        for rec in self:
            if rec.zoho_bill_id:
                rec.zoho_state = 'done'
                continue
            job = Job.search([('res_model', '=', rec._name), ('res_id', '=', rec.id), ('kind', '=', 'bill')], limit=1)
            rec.zoho_state = job.state if job else 'none'

    def _cp_zoho_account(self):
        account = self.env['cp.zoho.account'].sudo().cp_for(self.company_id)
        return account if account and account.use_books else self.env['cp.zoho.account']

    def _cp_use_odoo_vendor_bill(self):
        account = self._cp_zoho_account()
        if account and account.skip_odoo_bill:
            return False
        return super()._cp_use_odoo_vendor_bill()

    def action_approve(self):
        res = super().action_approve()
        Job = self.env['cp.zoho.sync']
        for rec in self:
            account = rec._cp_zoho_account()
            if not account:
                continue
            Job.cp_enqueue(account, 'bill', rec)
            if account.push_invoice_freight:
                for order in rec.line_ids.trip_id.order_ids.filtered('invoice_ref'):
                    Job.cp_enqueue(account, 'invoice_freight', order)
        return res

    def action_send_zoho(self):
        for rec in self:
            account = rec._cp_zoho_account()
            if not account:
                raise UserError(_('Connect Zoho Books first (Setup > Zoho).'))
            if rec.state != 'approved':
                raise UserError(_('Only approved bills go to Zoho Books.'))
            self.env['cp.zoho.sync'].cp_enqueue(account, 'bill', rec).cp_run()
        return True


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    zoho_creator_id = fields.Char(string='Zoho Creator ID', copy=False, readonly=True)

    def _cp_zoho_creator_push(self):
        Job = self.env['cp.zoho.sync']
        for rec in self:
            account = self.env['cp.zoho.account'].sudo().cp_for(rec.company_id)
            if account and account.use_creator and rec.state != 'draft':
                Job.cp_enqueue(account, 'creator', rec)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._cp_zoho_creator_push()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('cp_zoho_skip') and CREATOR_FIELDS & set(vals):
            self._cp_zoho_creator_push()
        return res

    def action_send_zoho_creator(self):
        account = self.env['cp.zoho.account'].cp_for(self.company_id[:1] or self.env.company)
        if not account or not account.use_creator:
            raise UserError(_('Turn on Zoho Creator on the Zoho connection first.'))
        for rec in self:
            self.env['cp.zoho.sync'].cp_enqueue(account, 'creator', rec).cp_run()
        return True


class CpTripOrder(models.Model):
    _inherit = 'cp.trip.order'

    zoho_invoice_id = fields.Char(string='Zoho Invoice ID', copy=False, readonly=True)
    zoho_freight_synced = fields.Monetary(string='Freight in Zoho', copy=False, readonly=True)
