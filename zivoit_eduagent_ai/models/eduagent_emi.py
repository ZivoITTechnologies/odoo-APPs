# -*- coding: utf-8 -*-
"""Fee & EMI agent: instalment schedules on sales orders, payment tracking against the order's
invoices (no extra accounting entries), AI-written reminders and refund-risk flags."""
import json
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round, html_escape
from odoo.tools.misc import formatLang


class EduagentEmiPlan(models.Model):
    _name = "eduagent.emi.plan"
    _description = "EMI / Instalment Plan"
    _order = "installment_count"

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)
    installment_count = fields.Integer(string="Instalments", default=3, required=True)
    interval_months = fields.Integer(string="Every (months)", default=1, required=True)
    down_payment_pct = fields.Float(string="Down Payment %", default=0.0)
    note = fields.Text(translate=True)


class EduagentInstallment(models.Model):
    _name = "eduagent.installment"
    _description = "Fee Instalment"
    _order = "order_id, sequence"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one(related="order_id.partner_id", store=True, index=True)
    currency_id = fields.Many2one(related="order_id.currency_id", store=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True)
    sequence = fields.Integer()
    name = fields.Char(compute="_compute_name")
    due_date = fields.Date(required=True)
    amount = fields.Monetary(required=True)
    cumulative_due = fields.Monetary(compute="_compute_state", store=True)
    state = fields.Selection([("planned", "Planned"), ("due", "Due Soon"), ("paid", "Paid"),
                              ("overdue", "Overdue")], compute="_compute_state", store=True, index=True)
    last_reminder = fields.Datetime(readonly=True)
    reminder_count = fields.Integer(readonly=True)

    @api.depends("sequence", "order_id")
    def _compute_name(self):
        for rec in self:
            total = len(rec.order_id.eduagent_installment_ids)
            rec.name = _("%(o)s - instalment %(i)s/%(n)s", o=rec.order_id.name, i=rec.sequence, n=total)

    @api.depends("amount", "due_date", "order_id.eduagent_amount_paid")
    def _compute_state(self):
        today = fields.Date.context_today(self)
        for rec in self:
            prior = rec.order_id.eduagent_installment_ids.filtered(lambda i: i.sequence <= rec.sequence)
            cumulative = sum(prior.mapped("amount"))
            rec.cumulative_due = cumulative
            paid = rec.order_id.eduagent_amount_paid
            if paid + 0.01 >= cumulative:
                rec.state = "paid"
            elif rec.due_date and rec.due_date < today:
                rec.state = "overdue"
            elif rec.due_date and rec.due_date <= today + timedelta(days=5):
                rec.state = "due"
            else:
                rec.state = "planned"

    def _send_reminder(self):
        agent = self.env.ref("zivoit_eduagent_ai.agent_fee", raise_if_not_found=False)
        for rec in self:
            partner = rec.partner_id
            if not partner.email:
                continue
            invoices = rec.order_id.invoice_ids.filtered(
                lambda m: m.state == "posted" and m.payment_state in ("not_paid", "partial"))
            link = (rec.get_base_url() + invoices[:1].get_portal_url()) if invoices else \
                (rec.get_base_url() + rec.order_id.get_portal_url())
            facts = {"name": (partner.name or "").split(" ")[0], "amount": rec.amount,
                     "currency": rec.currency_id.name, "due_date": str(rec.due_date),
                     "state": rec.state, "programme": rec.order_id.order_line[:1].name or rec.order_id.name}
            mock = {"subject": _("Fee instalment %s", rec.name),
                    "body": _("Hi %(n)s, a friendly reminder that your instalment of %(a)s is due on %(d)s. "
                              "If you need a different plan, just reply and we'll help.",
                              n=facts["name"], a=formatLang(self.env, rec.amount, currency_obj=rec.currency_id),
                              d=rec.due_date)}
            res = self.env["eduagent.llm"].complete(
                "Write a polite, empathetic fee reminder (max 80 words). Offer help if they face difficulty. "
                'Never threaten. Answer JSON {"subject":"","body":""}.',
                [{"role": "user", "content": json.dumps(facts)}], agent=agent, tier="fast", json_mode=True,
                purpose="Fee reminder", partner=partner, mock_response=json.dumps(mock))
            data = res["data"] if res.get("ok") else mock
            body = html_escape(data.get("body") or mock["body"]).replace("\n", "<br/>")
            self.env["mail.mail"].sudo().create({
                "subject": data.get("subject") or mock["subject"],
                "body_html": "<p>%s</p><p><a href='%s'>%s</a></p>" % (body, link, _("View and pay")),
                "email_to": partner.email, "recipient_ids": [(4, partner.id)],
            })
            rec.write({"last_reminder": fields.Datetime.now(), "reminder_count": rec.reminder_count + 1})

    @api.model
    def _cron_eduagent_fees(self):
        recs = self.search([("state", "!=", "paid")])
        # date-dependent states: force a recompute
        self.env.add_to_compute(self._fields["state"], recs)
        self.env.add_to_compute(self._fields["cumulative_due"], recs)
        self.env.flush_all()
        now = fields.Datetime.now()
        todo = recs.filtered(lambda r: r.state in ("due", "overdue") and
                             (not r.last_reminder or r.last_reminder < now - timedelta(days=4)))
        todo._send_reminder()


class SaleOrder(models.Model):
    _inherit = "sale.order"

    eduagent_emi_plan_id = fields.Many2one("eduagent.emi.plan", string="EMI Plan")
    eduagent_installment_ids = fields.One2many("eduagent.installment", "order_id", string="Instalments")
    eduagent_amount_paid = fields.Monetary(compute="_compute_eduagent_paid", store=True, string="Amount Paid")
    eduagent_refund_risk = fields.Selection([("low", "Low"), ("medium", "Medium"), ("high", "High")],
                                            compute="_compute_eduagent_refund_risk", string="Refund Risk")
    eduagent_refund_reasons = fields.Text(compute="_compute_eduagent_refund_risk", string="Refund Risk Reasons")

    @api.depends("invoice_ids.amount_residual", "invoice_ids.state", "invoice_ids.amount_total")
    def _compute_eduagent_paid(self):
        for order in self:
            invoices = order.invoice_ids.filtered(lambda m: m.state == "posted")
            paid = sum(m.amount_total - m.amount_residual for m in invoices if m.move_type == "out_invoice")
            paid -= sum(m.amount_total - m.amount_residual for m in invoices if m.move_type == "out_refund")
            order.eduagent_amount_paid = paid

    def _compute_eduagent_refund_risk(self):
        Enrol = self.env["slide.channel.partner"].sudo()
        for order in self:
            reasons, score = [], 0
            overdue = order.eduagent_installment_ids.filtered(lambda i: i.state == "overdue")
            if overdue:
                score += 40
                reasons.append(_("%s overdue instalment(s)", len(overdue)))
            enrolments = Enrol.search([("partner_id", "=", order.partner_id.id)])
            high = enrolments.filtered(lambda e: e.eduagent_risk_level == "high")
            med = enrolments.filtered(lambda e: e.eduagent_risk_level == "medium")
            if high:
                score += 45
                reasons.append(_("High dropout risk in %s course(s)", len(high)))
            elif med:
                score += 25
                reasons.append(_("Medium dropout risk"))
            if enrolments and max(enrolments.mapped("completion") or [0]) < 10 and \
                    order.date_order and order.date_order < fields.Datetime.now() - timedelta(days=14):
                score += 20
                reasons.append(_("Less than 10% progress after 2 weeks"))
            order.eduagent_refund_risk = "high" if score >= 60 else ("medium" if score >= 30 else "low")
            order.eduagent_refund_reasons = "\n".join(reasons) or _("No warning signs")

    def action_eduagent_generate_schedule(self):
        for order in self:
            plan = order.eduagent_emi_plan_id
            if not plan:
                raise UserError(_("Choose an EMI plan first."))
            if plan.installment_count < 1:
                raise UserError(_("The plan needs at least one instalment."))
            order.eduagent_installment_ids.unlink()
            total = order.amount_total
            prec = order.currency_id.decimal_places
            down = float_round(total * (plan.down_payment_pct or 0.0) / 100.0, precision_digits=prec)
            rest = total - down
            each = float_round(rest / plan.installment_count, precision_digits=prec)
            start = fields.Date.context_today(order)
            lines, seq = [], 1
            if down:
                lines.append({"sequence": seq, "due_date": start, "amount": down})
                seq += 1
            for i in range(plan.installment_count):
                amount = each if i < plan.installment_count - 1 else float_round(
                    rest - each * (plan.installment_count - 1), precision_digits=prec)
                due = start + relativedelta(months=plan.interval_months * (i + (1 if down else 0)))
                lines.append({"sequence": seq, "due_date": due, "amount": amount})
                seq += 1
            order.eduagent_installment_ids = [(0, 0, v) for v in lines]
        return True

    def action_eduagent_send_reminders(self):
        self.eduagent_installment_ids.filtered(lambda i: i.state != "paid")._send_reminder()
        return True
