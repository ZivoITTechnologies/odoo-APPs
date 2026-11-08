import logging

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

from ..lib import queries as Q
from .order import parse_dt

_logger = logging.getLogger(__name__)


class ZivoShopifyPayout(models.Model):
    _name = "zivo.shopify.payout"
    _description = "Shopify Payout"
    _inherit = ["mail.thread"]
    _order = "issued_at desc, id desc"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(required=True, index=True, readonly=True)
    issued_at = fields.Datetime(readonly=True)
    shopify_status = fields.Char(readonly=True)
    currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    net_amount = fields.Monetary(readonly=True)
    gross_amount = fields.Monetary(compute="_compute_totals", store=True)
    fee_amount = fields.Monetary(compute="_compute_totals", store=True)
    line_ids = fields.One2many("zivo.shopify.payout.line", "payout_id", string="Transactions")
    move_id = fields.Many2one("account.move", string="Journal Entry", readonly=True, copy=False)
    state = fields.Selection([
        ("draft", "To Process"), ("posted", "Posted"), ("reconciled", "Reconciled"),
    ], default="draft", required=True, tracking=True)
    anomaly_count = fields.Integer(compute="_compute_anomalies", store=True, string="AI Anomalies")
    anomaly_note = fields.Text(compute="_compute_anomalies", store=True, string="AI Anomaly Report")
    effective_fee_percent = fields.Float(compute="_compute_totals", store=True, string="Effective Fee %")

    _gid_unique = models.Constraint("UNIQUE(instance_id, shopify_gid)", "Payout already imported.")

    @api.depends("line_ids.amount", "line_ids.fee", "line_ids.transaction_type")
    def _compute_totals(self):
        for payout in self:
            charges = payout.line_ids.filtered(lambda l: l.transaction_type == "CHARGE")
            payout.gross_amount = sum(payout.line_ids.mapped("amount"))
            payout.fee_amount = sum(payout.line_ids.mapped("fee"))
            base = sum(charges.mapped("amount"))
            payout.effective_fee_percent = (sum(charges.mapped("fee")) / base * 100) if base else 0.0

    @api.depends("line_ids.order_id", "line_ids.fee", "line_ids.amount", "net_amount", "line_ids.transaction_type")
    def _compute_anomalies(self):
        for payout in self:
            notes = []
            lines = payout.line_ids
            net = sum(lines.mapped("net"))
            if lines and abs(net - payout.net_amount) > 0.01:
                notes.append(self.env._("Transactions net to %(net).2f but the payout is %(payout).2f.", net=net, payout=payout.net_amount))
            charges = lines.filtered(lambda l: l.transaction_type == "CHARGE")
            for line in charges.filtered(lambda l: not l.order_id):
                notes.append(self.env._("Charge of %(amount).2f matches no Odoo order (%(reference)s).", amount=line.amount, reference=line.order_name or "-"))
            rates = [(line, line.fee / line.amount) for line in charges if line.amount > 0]
            if len(rates) >= 5:
                ordered = sorted(rate for _line, rate in rates)
                median = ordered[len(ordered) // 2]
                for line, rate in rates:
                    if median and rate > median * 2.5 and line.fee > 1:
                        notes.append(self.env._("Fee of %(fee).2f on %(reference)s is unusually high.", fee=line.fee, reference=line.order_name or line.shopify_gid))
            payout.anomaly_count = len(notes)
            payout.anomaly_note = "\n".join(notes) or False

    def action_process(self):
        for payout in self.filtered(lambda p: p.state == "draft"):
            payout._create_entry()
        return True

    def _create_entry(self):
        """Move the settled money out of the clearing account and book the fees.

        Works on Community: it relies on journal entries only, not on the Enterprise
        bank reconciliation widget.
        """
        self.ensure_one()
        instance = self.instance_id
        missing = [label for label, value in (
            (self.env._("Shopify Payments Journal"), instance.clearing_journal_id),
            (self.env._("Payout Bank Journal"), instance.payout_journal_id),
            (self.env._("Fees Account"), instance.fee_account_id),
        ) if not value]
        if missing:
            raise UserError(self.env._("Complete the store accounting settings: %s", ", ".join(missing)))
        clearing = instance._clearing_account()
        bank_account = instance.payout_journal_id.default_account_id
        company = instance.company_id
        date = self.issued_at.date() if self.issued_at else fields.Date.context_today(self)

        def line(account, amount, label, partner=False):
            balance = self.currency_id._convert(amount, company.currency_id, company, date)
            return Command.create({
                "account_id": account.id,
                "name": label,
                "currency_id": self.currency_id.id,
                "amount_currency": amount,
                "debit": balance if balance > 0 else 0.0,
                "credit": -balance if balance < 0 else 0.0,
                "partner_id": partner,
            })

        fee_total = sum(self.line_ids.mapped("fee"))
        commands = [line(bank_account, self.net_amount, f"{self.name} net payout")]
        if fee_total:
            commands.append(line(instance.fee_account_id, fee_total, f"{self.name} Shopify fees"))
        adjust_account = instance.adjustment_account_id or instance.fee_account_id
        for tx in self.line_ids:
            if tx.transaction_type in ("CHARGE", "REFUND"):
                commands.append(line(clearing, -tx.amount, f"{self.name} {tx.order_name or tx.transaction_type}", tx.order_id.partner_id.id or False))
            elif tx.amount:
                commands.append(line(adjust_account, -tx.amount, f"{self.name} {tx.raw_type}"))
        move = self.env["account.move"].with_company(company).create({
            "move_type": "entry",
            "journal_id": instance.clearing_journal_id.id,
            "date": date,
            "ref": self.name,
            "line_ids": commands,
        })
        move.action_post()
        self.write({"move_id": move.id, "state": "posted"})
        self._reconcile_charges(clearing)

    def _reconcile_charges(self, clearing):
        self.ensure_one()
        lines = self.move_id.line_ids.filtered(lambda l: l.account_id == clearing)
        orders = self.line_ids.order_id
        payments = orders.zivo_transaction_ids.payment_id
        counterpart = payments.move_id.line_ids.filtered(lambda l: l.account_id == clearing and not l.reconciled)
        if not counterpart:
            return
        (lines | counterpart).reconcile()
        if all(line.reconciled for line in lines):
            self.state = "reconciled"


class ZivoShopifyPayoutLine(models.Model):
    _name = "zivo.shopify.payout.line"
    _description = "Shopify Payout Transaction"
    _order = "id"

    payout_id = fields.Many2one("zivo.shopify.payout", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="payout_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="payout_id.currency_id")
    shopify_gid = fields.Char(required=True, index=True)
    transaction_type = fields.Selection([
        ("CHARGE", "Charge"), ("REFUND", "Refund"), ("ADJUSTMENT", "Adjustment"), ("DISPUTE", "Dispute"), ("OTHER", "Other"),
    ], required=True)
    raw_type = fields.Char()
    order_name = fields.Char()
    order_gid = fields.Char(index=True)
    order_id = fields.Many2one("sale.order", ondelete="set null", index=True)
    amount = fields.Monetary()
    fee = fields.Monetary()
    net = fields.Monetary()
    transaction_date = fields.Datetime()

    _gid_unique = models.Constraint("UNIQUE(payout_id, shopify_gid)", "Transaction already imported.")


class ZivoShopifyInstancePayouts(models.Model):
    _inherit = "zivo.shopify.instance"

    def _clearing_account(self):
        self.ensure_one()
        journal = self.clearing_journal_id
        accounts = journal.inbound_payment_method_line_ids.payment_account_id
        if not accounts:
            raise UserError(self.env._("Set an outstanding receipts account on journal %s.", journal.name))
        return accounts[:1]

    def action_setup_accounting(self):
        """Create the clearing journal, its account and the fee account in one click."""
        self.ensure_one()
        Account = self.env["account.account"].with_company(self.company_id)
        Journal = self.env["account.journal"].with_company(self.company_id)
        vals = {}
        if not self.clearing_journal_id:
            clearing = Account.create({
                "name": f"Shopify Clearing - {self.name}", "code": self._free_code(Account, "101950"),
                "account_type": "asset_current", "reconcile": True,
            })
            journal = Journal.create({
                "name": f"Shopify Payments - {self.name}", "type": "bank",
                "code": self._free_journal_code(Journal), "company_id": self.company_id.id,
                "currency_id": self.shop_currency_id.id if self.shop_currency_id != self.company_id.currency_id else False,
            })
            journal.inbound_payment_method_line_ids.write({"payment_account_id": clearing.id})
            journal.outbound_payment_method_line_ids.write({"payment_account_id": clearing.id})
            vals["clearing_journal_id"] = journal.id
        if not self.payout_journal_id:
            vals["payout_journal_id"] = Journal.search([("type", "=", "bank"), ("company_id", "=", self.company_id.id), ("id", "!=", vals.get("clearing_journal_id", 0))], limit=1).id
        if not self.fee_account_id:
            vals["fee_account_id"] = Account.create({
                "name": f"Shopify Fees - {self.name}", "code": self._free_code(Account, "641950"), "account_type": "expense",
            }).id
        self.write(vals)
        workflow = self.workflow_ids[:1]
        if workflow and not workflow.payment_journal_id:
            workflow.payment_journal_id = self.clearing_journal_id
        return self._notify(self.env._("Shopify accounting is ready."), "success")

    def _free_code(self, Account, start):
        code = int(start)
        while Account.search_count([("code", "=", str(code))]):
            code += 1
        return str(code)

    def _free_journal_code(self, Journal):
        index = 1
        while Journal.search_count([("code", "=", f"SHP{index}"), ("company_id", "=", self.company_id.id)]):
            index += 1
        return f"SHP{index}"

    def action_import_payouts(self):
        self.ensure_one()
        self._enqueue("import_payouts", "Import payouts", {}, dedupe_key="import_payouts")
        return self._notify(self.env._("Payout import queued."), "success")

    def _job_import_payouts(self, job):
        client = self._client()
        Payout = self.env["zivo.shopify.payout"]
        Currency = self.env["res.currency"].with_context(active_test=False)
        data = client.execute(Q.PAYOUTS, {}, expected_cost=500).get("shopifyPaymentsAccount")
        if not data:
            return "Shopify Payments is not active on this store"
        new = {}
        for node in data["payouts"]["nodes"]:
            payout = Payout.search([("instance_id", "=", self.id), ("shopify_gid", "=", node["id"])], limit=1)
            if payout:
                payout.shopify_status = node.get("status")
                continue
            new[node["id"]] = Payout.create({
                "name": f"Payout {node['id'].rsplit('/', 1)[-1]}",
                "instance_id": self.id,
                "shopify_gid": node["id"],
                "issued_at": parse_dt(node.get("issuedAt")),
                "shopify_status": node.get("status"),
                "currency_id": Currency.search([("name", "=", node["net"]["currencyCode"])], limit=1).id,
                "net_amount": float(node["net"]["amount"]),
            })
        if new:
            self._attach_payout_lines(client, new)
        return f"{len(new)} new payouts"

    def _attach_payout_lines(self, client, payouts):
        Line = self.env["zivo.shopify.payout.line"]
        Order = self.env["sale.order"]
        pending = set(payouts)
        known = {"CHARGE", "REFUND", "ADJUSTMENT", "DISPUTE"}
        nodes = client.iter_connection(Q.BALANCE_TRANSACTIONS, {}, ["shopifyPaymentsAccount", "balanceTransactions"], max_pages=40)
        for node in nodes:
            payout_gid = (node.get("associatedPayout") or {}).get("id")
            if payout_gid not in payouts or node.get("test"):
                continue
            associated = node.get("associatedOrder") or {}
            source = node.get("sourceType") or node.get("type") or "OTHER"
            kind = source if source in known else ("ADJUSTMENT" if "ADJUSTMENT" in source else "OTHER")
            order = Order.search([("zivo_instance_id", "=", self.id), ("zivo_shopify_gid", "=", associated.get("id"))], limit=1) if associated.get("id") else Order
            Line.create({
                "payout_id": payouts[payout_gid].id,
                "shopify_gid": node["id"],
                "transaction_type": kind,
                "raw_type": node.get("type"),
                "order_name": associated.get("name"),
                "order_gid": associated.get("id"),
                "order_id": order.id,
                "amount": float(node["amount"]["amount"]),
                "fee": float(node["fee"]["amount"]),
                "net": float(node["net"]["amount"]),
                "transaction_date": parse_dt(node.get("transactionDate")),
            })
            pending.discard(payout_gid)
