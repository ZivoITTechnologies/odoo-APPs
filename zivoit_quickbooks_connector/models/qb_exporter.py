# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
"""Odoo -> QuickBooks export engine."""
import hashlib
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..lib.qbo_client import QBOError
from .qb_mapping import _parse_qb_datetime
from .qb_sync_job import QbConflict, QbSkip

_logger = logging.getLogger(__name__)

ODOO_TO_QB_ACCOUNT = {
    "asset_cash": ("Bank", "CashOnHand"),
    "asset_receivable": ("Accounts Receivable", "AccountsReceivable"),
    "asset_current": ("Other Current Asset", "OtherCurrentAssets"),
    "asset_prepayments": ("Other Current Asset", "PrepaidExpenses"),
    "asset_fixed": ("Fixed Asset", "OtherFixedAssets"),
    "asset_non_current": ("Other Asset", "OtherLongTermAssets"),
    "liability_payable": ("Accounts Payable", "AccountsPayable"),
    "liability_credit_card": ("Credit Card", "CreditCard"),
    "liability_current": ("Other Current Liability", "OtherCurrentLiabilities"),
    "liability_non_current": ("Long Term Liability", "OtherLongTermLiabilities"),
    "equity": ("Equity", "OpeningBalanceEquity"),
    "equity_unaffected": ("Equity", "RetainedEarnings"),
    "income": ("Income", "SalesOfProductIncome"),
    "income_other": ("Other Income", "OtherMiscellaneousIncome"),
    "expense": ("Expense", "OtherMiscellaneousServiceCost"),
    "expense_other": ("Other Expense", "OtherMiscellaneousExpense"),
    "expense_depreciation": ("Expense", "OtherMiscellaneousServiceCost"),
    "expense_direct_cost": ("Cost of Goods Sold", "SuppliesMaterialsCogs"),
    "off_balance": ("Other Asset", "OtherLongTermAssets"),
}
NAME_LIST = ("Customer", "Vendor", "Item", "ItemCategory", "Account", "Term", "Class", "Department")
QB_API_ENTITY = {"ItemCategory": "Item"}


class _Ctx:
    def __init__(self, conn, client, job=None):
        self.conn = conn
        self.client = client
        self.job = job
        self.depth = 0
        self.cache = {}


def _clean_name(name, size=100):
    name = re.sub(r"[:\t\n\r]", " ", name or "").strip()
    return re.sub(r"\s{2,}", " ", name)[:size] or "-"


def _q(value):
    """Escape a value for a QuickBooks query string literal."""
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def _money(value):
    return round(float(value or 0.0), 2)


class QbExporter(models.AbstractModel):
    _name = "qb.exporter"
    _description = "QuickBooks Export Engine"

    MOVE_TYPES_BY_ENTITY = {"Invoice": "out_invoice", "CreditMemo": "out_refund", "Bill": "in_invoice",
                            "VendorCredit": "in_refund", "JournalEntry": "entry"}

    # ================================================================ entry
    def _run_job(self, job):
        conn = job.connection_id
        env = self.with_company(conn.company_id)
        ctx = _Ctx(conn, conn._get_client(), job)
        if job.direction == "delete":
            return env._delete_remote(ctx, job.qb_entity, job.qb_id)
        record = self.env[job.res_model].browse(job.res_id).exists() if job.res_model else None
        if job.direction == "void":
            return env._void_remote(ctx, job.qb_entity, job.qb_id, record)
        if not record:
            raise QbSkip(_("The Odoo record no longer exists."))
        return env._export(ctx, job.qb_entity, record)

    @api.model
    def entity_for_move(self, move):
        for entity, mtype in self.MOVE_TYPES_BY_ENTITY.items():
            if move.move_type == mtype:
                return entity
        return None

    @api.model
    def entity_for_payment(self, payment):
        return "Payment" if payment.partner_type == "customer" else "BillPayment"

    def _export(self, ctx, entity, record):
        Mapping = self.env["qb.mapping"]
        builder = getattr(self, "_payload_%s" % entity.lower(), None)
        if not builder:
            raise QbSkip(_("Export of %s is not supported.", entity))
        mapping = Mapping._for_record(ctx.conn, record, entity)
        if mapping and mapping.state in ("deleted_qb", "voided_qb") and not self.env.context.get("qb_force"):
            raise QbSkip(_("Record was deleted/voided in QuickBooks; not re-created."))
        if not mapping and ctx.conn.match_existing and entity in NAME_LIST:
            existing = self._find_remote(ctx, entity, record)
            if existing:
                mapping = Mapping._link(ctx.conn, entity, existing, record, direction="export")
                mapping.state = "dirty"
        payload = builder(ctx, record)
        digest = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if mapping and mapping.qb_id and mapping.payload_hash == digest and not self.env.context.get("qb_force"):
            if ctx.depth:  # dependency already up to date: just reuse it
                return {"Id": mapping.qb_id, "SyncToken": mapping.qb_sync_token}
            raise QbSkip(_("No change since last export."), followup=lambda: mapping.write({"state": "synced"}))
        api_entity = QB_API_ENTITY.get(entity, entity)
        if mapping and mapping.qb_id:
            result = self._update_remote(ctx, entity, api_entity, mapping, record, payload)
        else:
            try:
                result = ctx.client.create(api_entity, payload)
            except QBOError as exc:
                if not exc.is_duplicate_name:
                    raise
                existing = self._find_remote(ctx, entity, record, by_name_only=True)
                if not existing:
                    raise
                mapping = Mapping._link(ctx.conn, entity, existing, record, direction="export")
                result = self._update_remote(ctx, entity, api_entity, mapping, record, payload)
        Mapping._link(ctx.conn, entity, result, record, direction="export", payload_hash=digest)
        if ctx.job:
            ctx.job.qb_id = result.get("Id")
        self._after_export(ctx, entity, record, result)
        return result

    def _update_remote(self, ctx, entity, api_entity, mapping, record, payload):
        body = dict(payload, Id=mapping.qb_id, SyncToken=mapping.qb_sync_token or "0")
        try:
            return ctx.client.update(api_entity, body)
        except QBOError as exc:
            if not exc.is_stale_object:
                raise
        # ---- conflict: QuickBooks changed since our last sync ----
        latest = ctx.client.read(api_entity, mapping.qb_id)
        policy = ctx.conn.conflict_policy
        force = self.env.context.get("qb_force")
        if not force:
            conn, qb_id = ctx.conn, mapping.qb_id

            def queue_import():
                conn._enqueue("import", entity, qb_id=qb_id, payload=latest, source="auto")
            if policy == "qb":
                raise QbSkip(_("QuickBooks version kept (conflict policy); import queued."), followup=queue_import)
            if policy == "newest":
                qb_dt = _parse_qb_datetime((latest.get("MetaData") or {}).get("LastUpdatedTime"))
                if qb_dt and record.write_date and qb_dt > record.write_date:
                    raise QbSkip(_("QuickBooks version is newer; import queued."), followup=queue_import)
            if policy == "manual":
                raise QbConflict(_("%(name)s was modified in QuickBooks since the last sync. Choose which "
                                   "version to keep.", name=record.display_name),
                                 followup=lambda: mapping.write({"state": "conflict"}))
        body["SyncToken"] = latest.get("SyncToken")
        return ctx.client.update(api_entity, body)

    def _after_export(self, ctx, entity, record, result):
        if entity in ("Invoice", "CreditMemo") and "qb_payment_link" in record._fields:
            link = result.get("InvoiceLink")
            if link:
                record.with_context(qb_importing=True).qb_payment_link = link

    # ========================================================= remote match
    def _find_remote(self, ctx, entity, record, by_name_only=False):
        client = ctx.client
        try:
            if entity in ("Customer", "Vendor"):
                name = self._partner_display_name(ctx, record, entity)
                res = client.query("SELECT * FROM %s WHERE DisplayName = '%s'" % (entity, _q(name))).get(entity)
                if not res and not by_name_only and record.email:
                    res = client.query("SELECT * FROM %s WHERE PrimaryEmailAddr = '%s'"
                                       % (entity, _q(record.email))).get(entity)
                return res[0] if res else None
            if entity == "Item":
                res = None
                if record.default_code and not by_name_only:
                    res = client.query("SELECT * FROM Item WHERE Sku = '%s'" % _q(record.default_code)).get("Item")
                if not res:
                    res = client.query("SELECT * FROM Item WHERE Name = '%s'"
                                       % _q(_clean_name(record.name))).get("Item")
                return res[0] if res else None
            if entity == "ItemCategory":
                res = client.query("SELECT * FROM Item WHERE Type = 'Category' AND Name = '%s'"
                                   % _q(_clean_name(record.name))).get("Item")
                return res[0] if res else None
            if entity == "Account":
                res = None
                if record.code and not by_name_only:
                    res = client.query("SELECT * FROM Account WHERE AcctNum = '%s'" % _q(record.code)).get("Account")
                if not res:
                    res = client.query("SELECT * FROM Account WHERE Name = '%s'"
                                       % _q(_clean_name(record.name))).get("Account")
                return res[0] if res else None
            if entity in ("Term", "Class", "Department"):
                res = client.query("SELECT * FROM %s WHERE Name = '%s'"
                                   % (entity, _q(_clean_name(record.name)))).get(entity)
                return res[0] if res else None
        except QBOError:
            _logger.info("QuickBooks lookup failed", exc_info=True)
        return None

    # ===================================================== reference helpers
    def _qb_ref(self, ctx, entity, record, required=True):
        """Return {'value': qb_id} for an Odoo record, exporting it first if needed."""
        if not record:
            return None
        Mapping = self.env["qb.mapping"]
        mapping = Mapping._for_record(ctx.conn, record, entity)
        if mapping and mapping.qb_id and mapping.state not in ("deleted_qb", "unmapped"):
            return {"value": mapping.qb_id}
        if entity in NAME_LIST and ctx.conn.match_existing:
            existing = self._find_remote(ctx, entity, record)
            if existing:
                Mapping._link(ctx.conn, entity, existing, record, direction="export")
                return {"value": existing["Id"]}
        if not ctx.conn._allows(entity, "export"):
            if required:
                raise UserError(_("%(model)s '%(name)s' is not linked to QuickBooks. Map it in QuickBooks > "
                                  "Mappings or enable export for %(entity)s.", model=record._description,
                                  name=record.display_name, entity=entity))
            return None
        if ctx.depth > 5:
            raise UserError(_("Dependency chain too deep while exporting %s.", record.display_name))
        ctx.depth += 1
        try:
            result = self._export(ctx, entity, record)
        except QbSkip:
            mapping = Mapping._for_record(ctx.conn, record, entity)
            return {"value": mapping.qb_id} if mapping and mapping.qb_id else None
        finally:
            ctx.depth -= 1
        return {"value": result["Id"]}

    def _account_ref(self, ctx, account, required=True):
        return self._qb_ref(ctx, "Account", account, required=required) if account else None

    def _default_account_ref(self, ctx, qb_type, subtype=None):
        key = ("acc", qb_type, subtype)
        if key not in ctx.cache:
            where = "AccountType = '%s'" % qb_type
            if subtype:
                where += " AND AccountSubType = '%s'" % subtype
            res = ctx.client.query("SELECT * FROM Account WHERE %s MAXRESULTS 1" % where).get("Account")
            ctx.cache[key] = {"value": res[0]["Id"]} if res else None
        return ctx.cache[key]

    def _tax_code_ref(self, ctx, taxes, use="sale"):
        if ctx.conn.is_us_company:
            return {"value": "TAX" if taxes else "NON"}
        if not taxes:
            return None
        entity = "TaxCode" if use == "sale" else "TaxCodePurchase"
        mapping = self.env["qb.mapping"]._for_record(ctx.conn, taxes[:1], entity)
        if not mapping or not mapping.qb_id:
            raise UserError(_("Tax '%(tax)s' is not mapped to a QuickBooks tax code. Import QuickBooks taxes or run "
                              "Smart Match, then map it in QuickBooks > Mappings.", tax=taxes[:1].display_name))
        return {"value": mapping.qb_id}

    def _analytic_refs(self, ctx, distribution):
        class_ref = dept_ref = None
        if distribution:
            ids = [int(i) for key in distribution for i in str(key).split(",") if i.strip().isdigit()]
            for acc in self.env["account.analytic.account"].browse(ids).exists():
                if acc.plan_id == ctx.conn.class_plan_id and not class_ref:
                    class_ref = self._qb_ref(ctx, "Class", acc, required=False)
                elif acc.plan_id == ctx.conn.location_plan_id and not dept_ref:
                    dept_ref = self._qb_ref(ctx, "Department", acc, required=False)
        return class_ref, dept_ref

    def _currency_vals(self, ctx, currency, rate=None):
        company_currency = ctx.conn.company_id.currency_id
        if not currency or currency == company_currency:
            return {}
        vals = {"CurrencyRef": {"value": currency.name}}
        if rate:
            vals["ExchangeRate"] = round(rate, 8)
        return vals

    @staticmethod
    def _addr(partner):
        if not partner:
            return None
        addr = {"Line1": partner.street or "", "Line2": partner.street2 or "", "City": partner.city or "",
                "CountrySubDivisionCode": partner.state_id.code or "", "PostalCode": partner.zip or "",
                "Country": partner.country_id.code or ""}
        return {k: v for k, v in addr.items() if v} or None

    # ========================================================== master data
    def _partner_display_name(self, ctx, partner, entity):
        name = _clean_name(partner.name, 450)
        if entity == "Vendor" and partner.customer_rank and ctx.conn.vendor_suffix:
            name = name + ctx.conn.vendor_suffix
        return name

    def _payload_partner(self, ctx, partner, entity):
        payload = {
            "DisplayName": self._partner_display_name(ctx, partner, entity),
            "Active": bool(partner.active),
        }
        if partner.is_company:
            payload["CompanyName"] = _clean_name(partner.name, 100)
        else:
            parts = (partner.name or "").split(" ", 1)
            payload["GivenName"] = parts[0][:100]
            if len(parts) > 1:
                payload["FamilyName"] = parts[1][:100]
            if partner.parent_id.is_company:
                payload["CompanyName"] = _clean_name(partner.parent_id.name, 100)
        if partner.email:
            payload["PrimaryEmailAddr"] = {"Address": partner.email.split(",")[0].strip()}
        if partner.phone:
            payload["PrimaryPhone"] = {"FreeFormNumber": partner.phone[:30]}
        if partner.website:
            payload["WebAddr"] = {"URI": partner.website[:1000]}
        if partner.comment:
            payload["Notes"] = html2plaintext(partner.comment)[:2000]
        bill = self._addr(partner)
        if bill:
            payload["BillAddr"] = bill
        if entity == "Customer":
            ship = partner.child_ids.filtered(lambda c: c.type == "delivery")[:1]
            if ship and self._addr(ship):
                payload["ShipAddr"] = self._addr(ship)
            term = partner.property_payment_term_id
            if term:
                ref = self._qb_ref(ctx, "Term", term, required=False)
                if ref:
                    payload["SalesTermRef"] = ref
            if partner.vat:
                payload["PrimaryTaxIdentifier"] = partner.vat[:20]
        else:
            term = partner.property_supplier_payment_term_id
            if term:
                ref = self._qb_ref(ctx, "Term", term, required=False)
                if ref:
                    payload["TermRef"] = ref
            if partner.ref:
                payload["AcctNum"] = partner.ref[:100]
            if partner.vat:
                payload["TaxIdentifier"] = partner.vat[:20]
        return payload

    def _payload_customer(self, ctx, partner):
        return self._payload_partner(ctx, partner, "Customer")

    def _payload_vendor(self, ctx, partner):
        return self._payload_partner(ctx, partner, "Vendor")

    def _payload_itemcategory(self, ctx, categ):
        payload = {"Name": _clean_name(categ.name), "Type": "Category"}
        if categ.parent_id and categ.parent_id.parent_id:  # skip the root "All"
            ref = self._qb_ref(ctx, "ItemCategory", categ.parent_id, required=False)
            if ref:
                payload.update(SubItem=True, ParentRef=ref)
        return payload

    def _payload_item(self, ctx, product):
        product = product.with_company(ctx.conn.company_id)
        is_service = product.type == "service"
        is_storable = bool(product._fields.get("is_storable") and product.is_storable)
        income = product.property_account_income_id or product.categ_id.property_account_income_categ_id \
            or ctx.conn.income_account_id
        expense = product.property_account_expense_id or product.categ_id.property_account_expense_categ_id \
            or ctx.conn.expense_account_id
        income_ref = self._account_ref(ctx, income, required=False) or \
            self._default_account_ref(ctx, "Income")
        expense_ref = self._account_ref(ctx, expense, required=False) or \
            self._default_account_ref(ctx, "Cost of Goods Sold") or self._default_account_ref(ctx, "Expense")
        payload = {
            "Name": _clean_name(product.display_name if product.product_template_variant_value_ids else product.name),
            "Type": "Service" if is_service else ("Inventory" if is_storable else "NonInventory"),
            "Active": bool(product.active),
            "UnitPrice": product.lst_price,
            "PurchaseCost": product.standard_price,
        }
        if income_ref:
            payload["IncomeAccountRef"] = income_ref
        if expense_ref and product.purchase_ok:
            payload["ExpenseAccountRef"] = expense_ref
        if product.default_code:
            payload["Sku"] = product.default_code[:100]
        if product.description_sale:
            payload["Description"] = product.description_sale[:4000]
        if product.description_purchase:
            payload["PurchaseDesc"] = product.description_purchase[:1000]
        if ctx.conn.is_us_company:
            payload["Taxable"] = bool(product.taxes_id)
        else:
            sale_tax = product.taxes_id.filtered(lambda t: t.company_id == ctx.conn.company_id)[:1]
            if sale_tax:
                ref = self._tax_code_ref_safe(ctx, sale_tax, "sale")
                if ref:
                    payload["SalesTaxCodeRef"] = ref
        if product.categ_id and product.categ_id.parent_id and ctx.conn._allows("ItemCategory", "export"):
            ref = self._qb_ref(ctx, "ItemCategory", product.categ_id, required=False)
            if ref:
                payload.update(SubItem=True, ParentRef=ref)
        if payload["Type"] == "Inventory":
            asset_ref = self._default_account_ref(ctx, "Other Current Asset", "Inventory")
            mapping = self.env["qb.mapping"]._for_record(ctx.conn, product, "Item")
            if asset_ref:
                payload.update(TrackQtyOnHand=True, AssetAccountRef=asset_ref)
                if not (mapping and mapping.qb_id):
                    payload["QtyOnHand"] = float(product.qty_available) if "qty_available" in product._fields else 0.0
                    payload["InvStartDate"] = fields.Date.to_string(fields.Date.context_today(self))
            else:
                payload["Type"] = "NonInventory"
        return payload

    def _tax_code_ref_safe(self, ctx, taxes, use):
        try:
            return self._tax_code_ref(ctx, taxes, use)
        except UserError:
            return None

    def _payload_account(self, ctx, account):
        qb_type, sub = ODOO_TO_QB_ACCOUNT.get(account.account_type, ("Other Current Asset", None))
        payload = {"Name": _clean_name(account.name), "AccountType": qb_type, "Active": bool(account.active)}
        if sub:
            payload["AccountSubType"] = sub
        if account.code and self.env.context.get("qb_send_acct_num", True):
            payload["AcctNum"] = account.code[:20]
        if account.currency_id and account.currency_id != ctx.conn.company_id.currency_id:
            payload["CurrencyRef"] = {"value": account.currency_id.name}
        return payload

    def _payload_term(self, ctx, term):
        days = max(term.line_ids.mapped("nb_days") or [0])
        payload = {"Name": _clean_name(term.name, 31), "Active": bool(term.active), "Type": "STANDARD",
                   "DueDays": days}
        if term.early_discount:
            payload.update(DiscountPercent=term.discount_percentage, DiscountDays=term.discount_days)
        return payload

    def _payload_class(self, ctx, analytic):
        return {"Name": _clean_name(analytic.name), "Active": bool(analytic.active)}

    def _payload_department(self, ctx, analytic):
        return {"Name": _clean_name(analytic.name), "Active": bool(analytic.active)}

    # ========================================================= transactions
    def _sales_line(self, ctx, line, use="sale"):
        if line.display_type in ("line_section", "line_note", "line_subsection"):
            return {"DetailType": "DescriptionOnly", "Description": (line.name or "")[:4000],
                    "DescriptionLineDetail": {}}
        product = line.product_id or self.env["qb.importer"]._misc_product(None)
        qty = line.quantity or 1.0
        amount = line.price_subtotal
        detail = {"ItemRef": self._qb_ref(ctx, "Item", product), "Qty": qty,
                  "UnitPrice": round(amount / qty, 7) if qty else amount}
        tax_ref = self._tax_code_ref(ctx, line.tax_ids, use)
        if tax_ref:
            detail["TaxCodeRef"] = tax_ref
        class_ref, _dept = self._analytic_refs(ctx, line.analytic_distribution)
        if class_ref:
            detail["ClassRef"] = class_ref
        if "deferred_start_date" in line._fields and line.deferred_start_date:
            detail["ServiceDate"] = fields.Date.to_string(line.deferred_start_date)
        return {"DetailType": "SalesItemLineDetail", "Amount": _money(amount),
                "Description": (line.name or "")[:4000], "SalesItemLineDetail": detail}

    def _expense_line(self, ctx, line):
        if line.display_type in ("line_section", "line_note", "line_subsection"):
            return None
        amount = line.price_subtotal
        class_ref, _dept = self._analytic_refs(ctx, line.analytic_distribution)
        tax_ref = self._tax_code_ref(ctx, line.tax_ids, "purchase") if not ctx.conn.is_us_company else None
        if line.product_id and line.product_id.purchase_ok:
            qty = line.quantity or 1.0
            detail = {"ItemRef": self._qb_ref(ctx, "Item", line.product_id), "Qty": qty,
                      "UnitPrice": round(amount / qty, 7) if qty else amount}
            key = "ItemBasedExpenseLineDetail"
        else:
            account = line.account_id
            detail = {"AccountRef": self._account_ref(ctx, account)}
            key = "AccountBasedExpenseLineDetail"
        if tax_ref:
            detail["TaxCodeRef"] = tax_ref
        if class_ref:
            detail["ClassRef"] = class_ref
        return {"DetailType": key, "Amount": _money(amount), "Description": (line.name or "")[:4000], key: detail}

    def _doc_header(self, ctx, move, sale_side):
        partner = move.partner_id.commercial_partner_id
        entity = "Customer" if sale_side else "Vendor"
        payload = {
            ("CustomerRef" if sale_side else "VendorRef"): self._qb_ref(ctx, entity, partner),
            "TxnDate": fields.Date.to_string(move.invoice_date or move.date),
        }
        if move.invoice_date_due:
            payload["DueDate"] = fields.Date.to_string(move.invoice_date_due)
        number = move.name if sale_side else (move.ref or move.name)
        if ctx.conn.send_doc_number and number and number != "/":
            payload["DocNumber"] = number[-21:]
        rate = (1.0 / move.invoice_currency_rate) if move.invoice_currency_rate else None
        payload.update(self._currency_vals(ctx, move.currency_id, rate))
        if move.invoice_payment_term_id:
            ref = self._qb_ref(ctx, "Term", move.invoice_payment_term_id, required=False)
            if ref:
                payload["SalesTermRef"] = ref
        note = "Odoo %s" % move.name
        if move.ref and sale_side:
            note += " / %s" % move.ref
        payload["PrivateNote"] = note[:4000]
        _class, dept = self._analytic_refs(
            ctx, next((l.analytic_distribution for l in move.invoice_line_ids if l.analytic_distribution), None))
        if dept:
            payload["DepartmentRef"] = dept
        return payload

    def _tax_vals(self, ctx, move):
        if ctx.conn.is_us_company:
            if ctx.conn.us_send_tax_total and move.amount_tax:
                return {"TxnTaxDetail": {"TotalTax": _money(abs(move.amount_tax))}}
            return {}
        taxes = move.invoice_line_ids.tax_ids
        if not taxes:
            return {"GlobalTaxCalculation": "NotApplicable"}
        inclusive = all(t.price_include for t in taxes)
        return {"GlobalTaxCalculation": "TaxInclusive" if inclusive else "TaxExcluded"}

    def _payload_sales_doc(self, ctx, move):
        payload = self._doc_header(ctx, move, True)
        lines = [self._sales_line(ctx, l) for l in move.invoice_line_ids.sorted("sequence")]
        payload["Line"] = [l for l in lines if l]
        if move.narration:
            payload["CustomerMemo"] = {"value": html2plaintext(move.narration)[:1000]}
        email = move.partner_id.email
        if email:
            payload["BillEmail"] = {"Address": email.split(",")[0].strip()}
        payload.update(self._tax_vals(ctx, move))
        return payload

    def _payload_invoice(self, ctx, move):
        payload = self._payload_sales_doc(ctx, move)
        if move.partner_shipping_id and move.partner_shipping_id != move.partner_id:
            ship = self._addr(move.partner_shipping_id)
            if ship:
                payload["ShipAddr"] = ship
        return payload

    def _payload_creditmemo(self, ctx, move):
        return self._payload_sales_doc(ctx, move)

    def _payload_bill(self, ctx, move):
        payload = self._doc_header(ctx, move, False)
        payload.pop("SalesTermRef", None)
        if move.invoice_payment_term_id:
            ref = self._qb_ref(ctx, "Term", move.invoice_payment_term_id, required=False)
            if ref:
                payload["SalesTermRef"] = ref
        lines = [self._expense_line(ctx, l) for l in move.invoice_line_ids.sorted("sequence")]
        payload["Line"] = [l for l in lines if l]
        payload.update(self._tax_vals(ctx, move))
        return payload

    def _payload_vendorcredit(self, ctx, move):
        payload = self._payload_bill(ctx, move)
        payload.pop("DueDate", None)
        payload.pop("SalesTermRef", None)
        return payload

    def _payload_journalentry(self, ctx, move):
        lines = []
        foreign = move.currency_id != ctx.conn.company_id.currency_id
        for line in move.line_ids:
            amount = line.amount_currency if foreign else line.balance
            if not amount:
                continue
            detail = {"PostingType": "Debit" if amount > 0 else "Credit",
                      "AccountRef": self._account_ref(ctx, line.account_id)}
            if line.partner_id and line.account_type in ("asset_receivable", "liability_payable"):
                etype = "Customer" if line.account_type == "asset_receivable" else "Vendor"
                detail["Entity"] = {"Type": etype, "EntityRef": self._qb_ref(
                    ctx, etype, line.partner_id.commercial_partner_id)}
            class_ref, dept_ref = self._analytic_refs(ctx, line.analytic_distribution)
            if class_ref:
                detail["ClassRef"] = class_ref
            if dept_ref:
                detail["DepartmentRef"] = dept_ref
            lines.append({"DetailType": "JournalEntryLineDetail", "Amount": _money(abs(amount)),
                          "Description": (line.name or move.ref or "")[:4000], "JournalEntryLineDetail": detail})
        payload = {"TxnDate": fields.Date.to_string(move.date), "Line": lines,
                   "PrivateNote": ("Odoo %s %s" % (move.name, move.ref or "")).strip()[:4000]}
        if ctx.conn.send_doc_number and move.name and move.name != "/":
            payload["DocNumber"] = move.name[-21:]
        if foreign:
            rate = abs(move.line_ids[:1].balance / move.line_ids[:1].amount_currency) \
                if move.line_ids[:1].amount_currency else None
            payload.update(self._currency_vals(ctx, move.currency_id, rate))
        return payload

    def _payment_allocations(self, payment, entity):
        allocations = []
        if not payment.move_id:
            return allocations
        partials, _diff = payment.move_id._get_reconciled_invoices_partials()
        for _partial, amount, counterpart in partials:
            move = counterpart.move_id
            if move.move_type in ("out_invoice", "in_invoice") and move != payment.move_id:
                allocations.append((move, abs(amount)))
        if not allocations:
            for move in payment.invoice_ids.filtered(lambda m: m.state == "posted"):
                allocations.append((move, min(payment.amount, move.amount_total)))
        return allocations

    def _payload_payment(self, ctx, payment):
        partner = payment.partner_id.commercial_partner_id
        payload = {"CustomerRef": self._qb_ref(ctx, "Customer", partner), "TotalAmt": _money(payment.amount),
                   "TxnDate": fields.Date.to_string(payment.date)}
        if payment.memo:
            payload["PaymentRefNum"] = payment.memo[:21]
        payload.update(self._currency_vals(ctx, payment.currency_id))
        deposit = payment.journal_id.default_account_id
        ref = self._account_ref(ctx, deposit, required=False) if deposit else None
        if ref:
            payload["DepositToAccountRef"] = ref
        pm = self.env["qb.mapping"]._for_record(ctx.conn, payment.journal_id, "PaymentMethod")
        if pm and pm.qb_id:
            payload["PaymentMethodRef"] = {"value": pm.qb_id}
        lines = []
        for move, amount in self._payment_allocations(payment, "Invoice"):
            lines.append({"Amount": _money(amount), "LinkedTxn": [
                {"TxnId": self._qb_ref(ctx, "Invoice", move)["value"], "TxnType": "Invoice"}]})
        if lines:
            payload["Line"] = lines
        return payload

    def _payload_billpayment(self, ctx, payment):
        partner = payment.partner_id.commercial_partner_id
        bank_ref = self._account_ref(ctx, payment.journal_id.default_account_id)
        is_cc = payment.journal_id.type == "credit"
        payload = {"VendorRef": self._qb_ref(ctx, "Vendor", partner), "TotalAmt": _money(payment.amount),
                   "TxnDate": fields.Date.to_string(payment.date),
                   "PayType": "CreditCard" if is_cc else "Check"}
        if is_cc:
            payload["CreditCardPayment"] = {"CCAccountRef": bank_ref}
        else:
            payload["CheckPayment"] = {"BankAccountRef": bank_ref, "PrintStatus": "NotSet"}
        if payment.memo:
            payload["DocNumber"] = payment.memo[:21]
        payload.update(self._currency_vals(ctx, payment.currency_id))
        lines = []
        for move, amount in self._payment_allocations(payment, "Bill"):
            lines.append({"Amount": _money(amount), "LinkedTxn": [
                {"TxnId": self._qb_ref(ctx, "Bill", move)["value"], "TxnType": "Bill"}]})
        payload["Line"] = lines
        if not lines:
            raise UserError(_("QuickBooks requires a vendor payment to be applied to at least one bill. "
                              "Reconcile %s with a bill first.", payment.display_name))
        return payload

    def _payload_estimate(self, ctx, order):
        partner = order.partner_id.commercial_partner_id
        status = {"draft": "Pending", "sent": "Pending", "sale": "Accepted", "cancel": "Rejected"}.get(
            order.state, "Pending")
        lines = []
        for line in order.order_line.sorted("sequence"):
            if line.display_type:
                lines.append({"DetailType": "DescriptionOnly", "Description": (line.name or "")[:4000],
                              "DescriptionLineDetail": {}})
                continue
            qty = line.product_uom_qty or 1.0
            detail = {"ItemRef": self._qb_ref(ctx, "Item", line.product_id), "Qty": qty,
                      "UnitPrice": round(line.price_subtotal / qty, 7)}
            tax_ref = self._tax_code_ref(ctx, line.tax_ids, "sale")
            if tax_ref:
                detail["TaxCodeRef"] = tax_ref
            lines.append({"DetailType": "SalesItemLineDetail", "Amount": _money(line.price_subtotal),
                          "Description": (line.name or "")[:4000], "SalesItemLineDetail": detail})
        payload = {"CustomerRef": self._qb_ref(ctx, "Customer", partner),
                   "TxnDate": fields.Date.to_string(order.date_order.date()), "TxnStatus": status,
                   "Line": lines, "PrivateNote": ("Odoo %s" % order.name)[:4000]}
        if order.validity_date:
            payload["ExpirationDate"] = fields.Date.to_string(order.validity_date)
        if ctx.conn.send_doc_number:
            payload["DocNumber"] = order.name[-21:]
        payload.update(self._currency_vals(ctx, order.currency_id))
        if ctx.conn.is_us_company and ctx.conn.us_send_tax_total and order.amount_tax:
            payload["TxnTaxDetail"] = {"TotalTax": _money(order.amount_tax)}
        return payload

    def _payload_purchaseorder(self, ctx, order):
        partner = order.partner_id.commercial_partner_id
        lines = []
        for line in order.order_line.sorted("sequence"):
            if line.display_type:
                continue
            qty = line.product_qty or 1.0
            detail = {"ItemRef": self._qb_ref(ctx, "Item", line.product_id), "Qty": qty,
                      "UnitPrice": round(line.price_subtotal / qty, 7)}
            if not ctx.conn.is_us_company:
                tax_ref = self._tax_code_ref(ctx, line.tax_ids, "purchase")
                if tax_ref:
                    detail["TaxCodeRef"] = tax_ref
            lines.append({"DetailType": "ItemBasedExpenseLineDetail", "Amount": _money(line.price_subtotal),
                          "Description": (line.name or "")[:4000], "ItemBasedExpenseLineDetail": detail})
        payload = {"VendorRef": self._qb_ref(ctx, "Vendor", partner),
                   "TxnDate": fields.Date.to_string(order.date_order.date()), "Line": lines,
                   "POStatus": "Closed" if getattr(order, "locked", False) else "Open",
                   "PrivateNote": ("Odoo %s" % order.name)[:4000]}
        if ctx.conn.send_doc_number:
            payload["DocNumber"] = order.name[-21:]
        payload.update(self._currency_vals(ctx, order.currency_id))
        ap = self._default_account_ref(ctx, "Accounts Payable")
        if ap:
            payload["APAccountRef"] = ap
        return payload

    # ========================================================= delete / void
    def _delete_remote(self, ctx, entity, qb_id):
        api_entity = QB_API_ENTITY.get(entity, entity)
        try:
            latest = ctx.client.read(api_entity, qb_id)
        except QBOError as exc:
            if exc.code in ("610", "620"):
                raise QbSkip(_("Already deleted in QuickBooks."))
            raise
        if not latest:
            raise QbSkip(_("Already deleted in QuickBooks."))
        if entity in NAME_LIST:
            ctx.client.update(api_entity, {"Id": qb_id, "SyncToken": latest["SyncToken"], "Active": False,
                                           **({"DisplayName": latest["DisplayName"]} if "DisplayName" in latest
                                              else {"Name": latest.get("Name")})})
        else:
            ctx.client.delete(api_entity, qb_id, latest["SyncToken"])
        mapping = self.env["qb.mapping"]._find(ctx.conn, entity, qb_id)
        if mapping:
            mapping.state = "deleted_odoo"
        return True

    def _void_remote(self, ctx, entity, qb_id, record):
        mapping = self.env["qb.mapping"]._find(ctx.conn, entity, qb_id)
        if not mapping:
            raise QbSkip(_("Document was never exported."))
        latest = ctx.client.read(entity, qb_id)
        if entity in ("Bill", "VendorCredit", "JournalEntry", "CreditMemo", "Estimate", "PurchaseOrder"):
            # these entities cannot be voided through the API: delete them
            ctx.client.delete(entity, qb_id, latest["SyncToken"])
            mapping.state = "deleted_odoo"
        else:
            result = ctx.client.void(entity, qb_id, latest["SyncToken"])
            if result:
                mapping.qb_sync_token = result.get("SyncToken")
            mapping.state = "voided_qb"
        return True
