# -*- coding: utf-8 -*-
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    jw_order_ids = fields.Many2many('zivo.job.work', compute='_compute_jw_variance', compute_sudo=True,
                                    string='Job Work Orders', groups='zivo_job_work_360.group_job_work_viewer')
    jw_is_job_work_bill = fields.Boolean('Job Work Bill', compute='_compute_jw_variance', compute_sudo=True)
    jw_has_variance = fields.Boolean('Job Work Variance', compute='_compute_jw_variance', compute_sudo=True)
    jw_qty_variance = fields.Float('Quantity Variance', compute='_compute_jw_variance', compute_sudo=True, digits='Product Unit')
    jw_price_variance = fields.Monetary('Rate Variance', compute='_compute_jw_variance', compute_sudo=True,
                                        currency_field='currency_id')
    jw_extra_amount = fields.Monetary('Unapproved Extra Charges', compute='_compute_jw_variance', compute_sudo=True,
                                      currency_field='currency_id')
    jw_value_variance = fields.Monetary('Value Variance', compute='_compute_jw_variance', compute_sudo=True,
                                        currency_field='currency_id')
    jw_variance_html = fields.Html('Job Work Check', compute='_compute_jw_variance', compute_sudo=True, sanitize=False)
    jw_review_state = fields.Selection([
        ('none', 'Not Reviewed'),
        ('approved', 'Variance Approved'),
        ('hold', 'On Hold'),
        ('dispute', 'Disputed'),
    ], 'Job Work Review', default='none', copy=False, tracking=True)

    @api.depends('invoice_line_ids.quantity', 'invoice_line_ids.price_unit', 'invoice_line_ids.purchase_line_id',
                 'invoice_line_ids.price_subtotal', 'move_type', 'currency_id', 'invoice_date')
    def _compute_jw_variance(self):
        for move in self:
            lines = move.invoice_line_ids.filtered(
                lambda l: l.display_type == 'product' and l.purchase_line_id.jw_order_id)
            jobs = lines.purchase_line_id.jw_order_id
            move.jw_order_ids = jobs
            move.jw_is_job_work_bill = bool(jobs)
            qty_var = price_var = value_var = extra = 0.0
            rows = []
            if move.move_type == 'in_invoice' and lines:
                billed_this = defaultdict(float)
                for line in lines:
                    billed_this[line.purchase_line_id.jw_order_id] += line.quantity
                for job in jobs:
                    company = job.company_id
                    other_lines = job.purchase_line_ids.invoice_lines.filtered(
                        lambda l: l.parent_state == 'posted' and l.display_type == 'product'
                        and l.move_id != move._origin)
                    billed_other = sum(
                        -l.quantity if l.move_id.move_type == 'in_refund' else l.quantity for l in other_lines)
                    billed_total = billed_other + billed_this[job]
                    allowed_qty = job.qty_accepted * (1 + company.jw_bill_qty_tolerance / 100.0)
                    job_qty_var = billed_total - allowed_qty
                    if job_qty_var > job.product_uom_id.rounding / 2.0:
                        qty_var += billed_total - job.qty_accepted
                    else:
                        job_qty_var = 0.0
                    job_lines = lines.filtered(lambda l, j=job: l.purchase_line_id.jw_order_id == j)
                    max_rate = job.rate * (1 + company.jw_bill_price_tolerance / 100.0)
                    job_price_var = 0.0
                    for line in job_lines:
                        price = line.price_unit
                        if line.currency_id and job.currency_id and line.currency_id != job.currency_id:
                            price = line.currency_id._convert(
                                price, job.currency_id, company, move.invoice_date or fields.Date.context_today(move))
                        if price - max_rate > job.currency_id.rounding:
                            diff = price - job.rate
                            job_price_var = max(job_price_var, diff)
                            value_var += diff * line.quantity
                    price_var = max(price_var, job_price_var)
                    if job_qty_var:
                        value_var += (billed_total - job.qty_accepted) * job.rate
                    rows.append((job, billed_total, job.qty_accepted, job.rate,
                                 max((line.price_unit for line in job_lines), default=0.0),
                                 bool(job_qty_var), bool(job_price_var)))
                other = move.invoice_line_ids.filtered(
                    lambda l: l.display_type == 'product' and not l.purchase_line_id)
                extra = sum(other.mapped('price_subtotal'))
            move.jw_qty_variance = qty_var
            move.jw_price_variance = price_var
            move.jw_extra_amount = extra
            move.jw_value_variance = value_var + extra
            move.jw_has_variance = bool(qty_var or price_var or extra)
            move.jw_variance_html = move._jw_render_variance(rows, extra) if rows else False

    def _jw_render_variance(self, rows, extra):
        self.ensure_one()
        body = Markup('')
        for job, billed, accepted, rate, billed_rate, qty_flag, price_flag in rows:
            body += Markup(
                '<tr><td>%s</td><td class="text-end %s">%s</td><td class="text-end">%s</td>'
                '<td class="text-end %s">%s</td><td class="text-end">%s</td></tr>') % (
                job.name, 'text-danger fw-bold' if qty_flag else '', billed, accepted,
                'text-danger fw-bold' if price_flag else '', self.currency_id.format(billed_rate),
                job.currency_id.format(rate))
        extra_html = Markup('')
        if extra:
            extra_html = Markup('<p class="text-danger mb-0">%s %s</p>') % (
                _("Charges not linked to the approved job:"), self.currency_id.format(extra))
        return Markup(
            '<table class="table table-sm mb-1"><thead><tr><th>%s</th><th class="text-end">%s</th>'
            '<th class="text-end">%s</th><th class="text-end">%s</th><th class="text-end">%s</th></tr></thead>'
            '<tbody>%s</tbody></table>%s') % (
            _("Job"), _("Billed Qty (incl. this bill)"), _("Accepted Qty"), _("Billed Rate"), _("Approved Rate"),
            body, extra_html)

    def _post(self, soft=True):
        for move in self.sudo().filtered(lambda m: m.move_type == 'in_invoice' and m.jw_is_job_work_bill):
            if move.jw_review_state in ('hold', 'dispute'):
                raise UserError(_(
                    "Vendor bill %(bill)s is %(state)s for job work review and cannot be posted.",
                    bill=move.ref or move.name or _('Draft'),
                    state=dict(self._fields['jw_review_state']._description_selection(self.env))[
                        move.jw_review_state]))
            if move.jw_has_variance and move.jw_review_state != 'approved' and any(
                    move.jw_order_ids.company_id.mapped('jw_block_bill_variance')):
                raise UserError(_(
                    "Vendor bill %(bill)s exceeds the approved job work: quantity variance %(qty)s, rate variance "
                    "%(rate)s, unapproved extra charges %(extra)s. A Job Work Administrator must approve the "
                    "variance, or put the bill on hold / dispute it.",
                    bill=move.ref or move.name or _('Draft'), qty=move.jw_qty_variance,
                    rate=move.currency_id.format(move.jw_price_variance),
                    extra=move.currency_id.format(move.jw_extra_amount)))
        res = super()._post(soft=soft)
        jobs = self.sudo().jw_order_ids
        if jobs:
            jobs.sudo()._jw_check_bill_variance()
        return res

    def _jw_check_manager(self):
        if not self.env.user.has_group('zivo_job_work_360.group_job_work_manager'):
            raise AccessError(_("Only Job Work Administrators can review job work bill variances."))

    def _jw_set_review(self, state, message):
        self._jw_check_manager()
        for move in self:
            move.jw_review_state = state
            move.message_post(body=message)
            for job in move.sudo().jw_order_ids:
                job.sudo().is_disputed = state == 'dispute' or any(
                    m.jw_review_state == 'dispute' for m in job.purchase_line_ids.invoice_lines.move_id - move)
                job.sudo().message_post(body=_("Vendor bill %(bill)s: %(msg)s",
                                               bill=move.ref or move.name, msg=message))
            move.sudo().jw_order_ids._jw_check_bill_variance()
        return True

    def action_jw_approve_variance(self):
        return self._jw_set_review('approved', _("Job work variance approved by %s.", self.env.user.name))

    def action_jw_hold(self):
        return self._jw_set_review('hold', _("Bill put on hold for job work review."))

    def action_jw_dispute(self):
        return self._jw_set_review('dispute', _("Bill disputed with the job worker."))

    def action_jw_reset_review(self):
        return self._jw_set_review('none', _("Job work review reset."))
