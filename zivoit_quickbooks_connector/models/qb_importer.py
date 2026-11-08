# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
"""QuickBooks -> Odoo import engine."""
import base64
import logging

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

from ..lib.qbo_client import QBOError
from .qb_mapping import _parse_qb_datetime
from .qb_sync_job import QbConflict, QbSkip

_logger = logging.getLogger(__name__)

QB_ACCOUNT_TYPES = {
    "Bank": "asset_cash",
    "Accounts Receivable": "asset_receivable",
    "Other Current Asset": "asset_current",
    "Fixed Asset": "asset_fixed",
    "Other Asset": "asset_non_current",
    "Accounts Payable": "liability_payable",
    "Credit Card": "liability_credit_card",
    "Other Current Liability": "liability_current",
    "Long Term Liability": "liability_non_current",
    "Equity": "equity",
    "Income": "income",
    "Other Income": "income_other",
    "Cost of Goods Sold": "expense_direct_cost",
    "Expense": "expense",
    "Other Expense": "expense",
}
MOVE_TYPE = {"Invoice": "out_invoice", "CreditMemo": "out_refund", "SalesReceipt": "out_invoice",
             "Bill": "in_invoice", "VendorCredit": "in_refund"}
SPECIAL_ITEMS = {"SHIPPING_ITEM_ID": "Shipping (QuickBooks)", "GRATUITY_ITEM_ID": "Gratuity (QuickBooks)"}
QB_READ_ENTITY = {"ItemCategory": "Item", "TaxCodePurchase": "TaxCode"}
MAX_DEPTH = 6


class _Ctx:
    """Per-job context passed through the import call chain."""

    def __init__(self, conn, client, job=None):
        self.conn = conn
        self.client = client
        self.job = job
        self.depth = 0
        self.rates = None


def _ref(value):
    return (value or {}).get("value") if isinstance(value, dict) else value


class QbImporter(models.AbstractModel):
    _name = "qb.importer"
    _description = "QuickBooks Import Engine"

    # ================================================================ entry
    def _run_job(self, job):
        conn = job.connection_id.with_company(job.connection_id.company_id)
        env = self.with_company(conn.company_id).with_context(qb_importing=True, tracking_disable=True,
                                                               mail_notrack=True, mail_create_nolog=True)
        ctx = _Ctx(conn, conn._get_client(), job)
        entity = job.qb_entity
        if job.operation == "Merge" and (job.payload or {}).get("merged_into"):
            return env._handle_merge(ctx, entity, job.qb_id, job.payload["merged_into"])
        if job.operation == "Delete":
            return env._handle_deleted(ctx, entity, job.qb_id)
        data = job.payload if job.payload and job.payload.get("Id") else None
        if data is None or job.source == "webhook":
            data = env._fetch(ctx, entity, job.qb_id)
            if data is None:
                return env._handle_deleted(ctx, entity, job.qb_id)
        record = env._import(ctx, entity, data)
        if record:
            job.write({"res_model": record._name, "res_id": record.id})
        return record

    def _fetch(self, ctx, entity, qb_id):
        try:
            return ctx.client.read(QB_READ_ENTITY.get(entity, entity), qb_id)
        except QBOError as exc:
            if exc.status in (400, 404) and exc.code in ("610", "620"):  # object not found / deleted
                return None
            raise

    def _import(self, ctx, entity, data):
        """Import one QBO object, returns the Odoo record."""
        if entity == "Item" and data.get("Type") == "Category":
            entity = "ItemCategory"
        handler = getattr(self, "_import_%s" % entity.lower(), None)
        if not handler:
            raise QbSkip(_("Import of %s is not supported.", entity))
        mapping = self.env["qb.mapping"]._find(ctx.conn, entity, data.get("Id"))
        record = None
        if mapping and mapping.res_model and mapping.res_id:
            record = self.env[mapping.res_model].browse(mapping.res_id).exists() or None
        if record is not None:
            self._check_conflict(ctx, entity, mapping, record, data)
        outer_voided, ctx.voided = getattr(ctx, "voided", False), False
        record = handler(ctx, data, record)
        voided, ctx.voided = ctx.voided, outer_voided
        if record is not None:
            mapping = self.env["qb.mapping"]._link(ctx.conn, entity, data, record, direction="import")
            if voided:
                mapping.state = "voided_qb"
        return record

    def _check_conflict(self, ctx, entity, mapping, record, data):
        if self.env.context.get("qb_force"):
            return
        token = str(data.get("SyncToken")) if data.get("SyncToken") is not None else None
        if mapping.state == "synced" and token and mapping.qb_sync_token == token:
            raise QbSkip(_("Already up to date (SyncToken %s).", token))
        if mapping.state not in ("dirty", "conflict"):
            return
        policy = ctx.conn.conflict_policy
        if policy == "qb":
            return
        if policy == "newest":
            qb_dt = _parse_qb_datetime((data.get("MetaData") or {}).get("LastUpdatedTime"))
            if qb_dt and record.write_date and qb_dt >= record.write_date:
                return
        conn = ctx.conn
        if policy in ("odoo", "newest"):
            raise QbSkip(_("Odoo version kept (conflict policy); export queued."),
                         followup=lambda: conn._enqueue("export", entity, record=record, source="auto"))
        raise QbConflict(_("%(entity)s %(name)s was changed both in Odoo and in QuickBooks. "
                           "Choose which version to keep.", entity=entity, name=record.display_name),
                         followup=lambda: mapping.write({"state": "conflict"}))

    def _ensure(self, ctx, entity, qb_id):
        """Return the Odoo record for a QBO reference, importing it on the fly."""
        qb_id = _ref(qb_id)
        if not qb_id:
            return None
        record = self.env["qb.mapping"]._find_record(ctx.conn, entity, qb_id)
        if record:
            return record
        mapping = self.env["qb.mapping"]._find(ctx.conn, entity, qb_id)
        if mapping and mapping.state == "unmapped" and entity in ("PaymentMethod",):
            return None
        if ctx.depth >= MAX_DEPTH:
            raise UserError(_("Dependency chain too deep while importing %s %s", entity, qb_id))
        data = self._fetch(ctx, entity, qb_id)
        if not data:
            return None
        ctx.depth += 1
        try:
            with self.env.cr.savepoint():
                return self.with_context(qb_force=True)._import(ctx, entity, data)
        except QbSkip:
            return self.env["qb.mapping"]._find_record(ctx.conn, entity, qb_id)
        finally:
            ctx.depth -= 1

    # ========================================================= references
    def _partner(self, ctx, ref, entity="Customer"):
        partner = self._ensure(ctx, entity, ref)
        if not partner and _ref(ref):
            raise UserError(_("QuickBooks %s %s could not be found.", entity, _ref(ref)))
        return partner

    def _product(self, ctx, ref):
        qb_id = _ref(ref)
        if not qb_id:
            return self._misc_product(ctx)
        if qb_id in SPECIAL_ITEMS:
            return self._special_product(ctx, qb_id)
        return self._ensure(ctx, "Item", qb_id) or self._misc_product(ctx)

    def _special_product(self, ctx, key):
        Product = self.env["product.product"].with_context(active_test=False)
        product = Product.search([("default_code", "=", "QB-%s" % key)], limit=1)
        if not product:
            product = Product.create({"name": SPECIAL_ITEMS[key], "default_code": "QB-%s" % key,
                                      "type": "service", "list_price": 0.0, "taxes_id": False,
                                      "supplier_taxes_id": False})
        return product

    def _misc_product(self, ctx):
        Product = self.env["product.product"].with_context(active_test=False)
        product = Product.search([("default_code", "=", "QB-MISC")], limit=1)
        if not product:
            product = Product.create({"name": "QuickBooks Item", "default_code": "QB-MISC", "type": "service",
                                      "taxes_id": False, "supplier_taxes_id": False})
        return product

    def _account(self, ctx, ref, required=False):
        account = self._ensure(ctx, "Account", ref)
        if not account and required:
            raise UserError(_("QuickBooks account %s is not mapped to an Odoo account.", _ref(ref)))
        return account

    def _currency(self, ctx, ref):
        code = _ref(ref)
        if not code:
            return ctx.conn.company_id.currency_id
        currency = self.env["res.currency"].with_context(active_test=False).search([("name", "=", code)], limit=1)
        if not currency:
            raise UserError(_("Currency %s does not exist in Odoo.", code))
        if not currency.active:
            currency.sudo().active = True
        return currency

    def _analytic(self, ctx, class_ref=None, dept_ref=None):
        dist = {}
        for entity, ref in (("Class", class_ref), ("Department", dept_ref)):
            acc = self._ensure(ctx, entity, ref) if _ref(ref) else None
            if acc:
                dist[str(acc.id)] = 100.0
        return dist or False

    def _tax_rates(self, ctx):
        if ctx.rates is None:
            ctx.rates = {r["Id"]: r for r in ctx.client.query_all("TaxRate")}
        return ctx.rates

    def _taxes(self, ctx, ref, use="sale"):
        code = _ref(ref)
        if not code or code in ("NON", "TAX") or ctx.conn.is_us_company:
            return self.env["account.tax"]
        entity = "TaxCode" if use == "sale" else "TaxCodePurchase"
        tax = self.env["qb.mapping"]._find_record(ctx.conn, entity, code)
        if tax:
            return tax
        data = self._fetch(ctx, "TaxCode", code)
        if data:
            self._import_taxcode(ctx, data, None)
            tax = self.env["qb.mapping"]._find_record(ctx.conn, entity, code)
        return tax or self.env["account.tax"]

    def _journal_for(self, ctx, account_ref=None, method_ref=None, default=None):
        mapping = self.env["qb.mapping"]._find(ctx.conn, "PaymentMethod", _ref(method_ref))
        if mapping and mapping.res_model == "account.journal" and mapping.res_id:
            return self.env["account.journal"].browse(mapping.res_id)
        account = self._account(ctx, account_ref) if _ref(account_ref) else None
        if account:
            journal = self.env["account.journal"].search([("default_account_id", "=", account.id),
                                                          ("company_id", "=", ctx.conn.company_id.id)], limit=1)
            if journal:
                return journal
        return default or ctx.conn.payment_journal_id

    # ========================================================= master data
    def _import_account(self, ctx, data, record):
        Account = self.env["account.account"].with_context(active_test=False)
        company = ctx.conn.company_id
        acc_type = QB_ACCOUNT_TYPES.get(data.get("AccountType"), "asset_current")
        name = data.get("Name") or data.get("FullyQualifiedName")
        code = (data.get("AcctNum") or "").strip()
        if record is None and ctx.conn.match_existing:
            if code:
                record = Account.search([("code", "=", code), ("company_ids", "in", company.ids)], limit=1) or None
            if record is None:
                record = Account.search([("name", "=ilike", name), ("company_ids", "in", company.ids)],
                                        limit=1) or None
        if record is None:
            if not code or Account.search_count([("code", "=", code), ("company_ids", "in", company.ids)]):
                code = self._free_account_code(company, data["Id"])
            vals = {"name": name, "code": code, "account_type": acc_type, "company_ids": [Command.set(company.ids)],
                    "reconcile": acc_type in ("asset_receivable", "liability_payable")}
            if data.get("CurrencyRef") and _ref(data["CurrencyRef"]) != company.currency_id.name:
                vals["currency_id"] = self._currency(ctx, data["CurrencyRef"]).id
            record = Account.create(vals)
        else:
            vals = {}
            if record.name != name:
                vals["name"] = name
            if "Active" in data and record.active != bool(data["Active"]):
                vals["active"] = bool(data["Active"])
            if vals:
                record.write(vals)
        if acc_type in ("asset_cash", "liability_credit_card") and data.get("Active", True):
            self._ensure_journal(ctx, record, "bank" if acc_type == "asset_cash" else "credit", name)
        return record

    def _free_account_code(self, company, qb_id):
        Account = self.env["account.account"].with_context(active_test=False)
        base = "QB%s" % qb_id
        code, i = base, 1
        while Account.search_count([("code", "=", code), ("company_ids", "in", company.ids)]):
            code = "%s%d" % (base, i)
            i += 1
        return code

    def _ensure_journal(self, ctx, account, jtype, name):
        Journal = self.env["account.journal"].with_context(active_test=False)
        company = ctx.conn.company_id
        if Journal.search_count([("default_account_id", "=", account.id), ("company_id", "=", company.id)]):
            return
        base = "QB%s" % "".join(c for c in (name or "BNK").upper() if c.isalnum())[:3]
        code, i = base[:5], 1
        while Journal.search_count([("code", "=", code), ("company_id", "=", company.id)]):
            code = "%s%d" % (base[:4], i)
            i += 1
        Journal.create({"name": name, "type": jtype, "code": code, "company_id": company.id,
                        "default_account_id": account.id})

    def _import_taxcode(self, ctx, data, record):
        if ctx.conn.is_us_company:
            # US automated sales tax: tax totals are imported as a tax line on documents
            self.env["qb.mapping"]._link(ctx.conn, "TaxCode", data, None, direction="import")
            return None
        rates = self._tax_rates(ctx)
        Tax = self.env["account.tax"].with_context(active_test=False)
        company = ctx.conn.company_id
        result = None
        for use, entity, key in (("sale", "TaxCode", "SalesTaxRateList"),
                                 ("purchase", "TaxCodePurchase", "PurchaseTaxRateList")):
            details = (data.get(key) or {}).get("TaxRateDetail") or []
            if not details:
                continue
            rate = sum(float(rates.get(_ref(d.get("TaxRateRef")), {}).get("RateValue") or 0) for d in details)
            existing = self.env["qb.mapping"]._find_record(ctx.conn, entity, data["Id"])
            name = "%s%s" % (data.get("Name"), "" if use == "sale" else " (Purchase)")
            if not existing and ctx.conn.match_existing:
                existing = Tax.search([("company_id", "=", company.id), ("type_tax_use", "=", use),
                                       ("amount_type", "=", "percent"), ("amount", "=", rate),
                                       ("name", "=ilike", data.get("Name"))], limit=1)
            if not existing:
                existing = Tax.search([("company_id", "=", company.id), ("type_tax_use", "=", use),
                                       ("name", "=", name)], limit=1)
            vals = {"name": name, "amount": rate, "amount_type": "percent", "type_tax_use": use,
                    "company_id": company.id, "active": bool(data.get("Active", True)),
                    "description": data.get("Description") or False}
            if existing:
                existing.write({k: v for k, v in vals.items() if k in ("amount", "active")})
            else:
                existing = Tax.create(vals)
            self.env["qb.mapping"]._link(ctx.conn, entity, data, existing, direction="import")
            result = result or existing
        if result is None:
            raise QbSkip(_("Tax code %s has no rates.", data.get("Name")))
        return result if result.type_tax_use == "sale" else None

    def _import_term(self, ctx, data, record):
        Term = self.env["account.payment.term"].with_context(active_test=False)
        name = data.get("Name")
        if record is None and ctx.conn.match_existing:
            record = Term.search([("name", "=ilike", name), ("company_id", "in", (False, ctx.conn.company_id.id))],
                                 limit=1) or None
        if data.get("DayOfMonthDue"):
            line = {"value": "percent", "value_amount": 100, "delay_type": "days_end_of_month_on_the",
                    "nb_days": 0, "days_next_month": str(data.get("DayOfMonthDue"))}
        else:
            line = {"value": "percent", "value_amount": 100, "delay_type": "days_after",
                    "nb_days": int(data.get("DueDays") or 0)}
        vals = {"name": name, "active": bool(data.get("Active", True)),
                "line_ids": [Command.clear(), Command.create(line)]}
        if data.get("DiscountPercent"):
            vals.update(early_discount=True, discount_percentage=float(data["DiscountPercent"]),
                        discount_days=int(data.get("DiscountDays") or 0))
        if record is None:
            record = Term.create(vals)
        else:
            record.write(vals)
        return record

    def _import_paymentmethod(self, ctx, data, record):
        if record is None:
            journal = self.env["account.journal"].search([("company_id", "=", ctx.conn.company_id.id),
                                                          ("name", "=ilike", data.get("Name")),
                                                          ("type", "in", ("bank", "cash", "credit"))], limit=1)
            if not journal:
                # kept as "Needs Mapping" so the user can link it to a journal
                self.env["qb.mapping"]._link(ctx.conn, "PaymentMethod", data, None, direction="import")
                return None
            record = journal
        return record

    def _import_analytic(self, ctx, data, record, plan):
        Analytic = self.env["account.analytic.account"].with_context(active_test=False)
        name = data.get("FullyQualifiedName") or data.get("Name")
        if record is None:
            record = Analytic.search([("plan_id", "=", plan.id), ("name", "=", name)], limit=1) or None
        vals = {"name": name, "plan_id": plan.id, "active": bool(data.get("Active", True))}
        if record is None:
            record = Analytic.create(vals)
        else:
            record.write(vals)
        return record

    def _import_class(self, ctx, data, record):
        return self._import_analytic(ctx, data, record, ctx.conn.class_plan_id)

    def _import_department(self, ctx, data, record):
        return self._import_analytic(ctx, data, record, ctx.conn.location_plan_id)

    def _address_vals(self, addr):
        if not addr:
            return {}
        vals = {"street": addr.get("Line1") or False,
                "street2": " ".join(filter(None, [addr.get(k) for k in ("Line2", "Line3", "Line4", "Line5")])) or False,
                "city": addr.get("City") or False, "zip": addr.get("PostalCode") or False}
        country = False
        cval = (addr.get("Country") or "").strip()
        if cval:
            Country = self.env["res.country"]
            country = Country.search(["|", ("code", "=ilike", cval), ("name", "=ilike", cval)], limit=1)
            if not country and cval.upper() in ("USA", "UNITED STATES OF AMERICA"):
                country = self.env.ref("base.us", raise_if_not_found=False)
        vals["country_id"] = country.id if country else False
        sub = (addr.get("CountrySubDivisionCode") or "").strip()
        if sub:
            dom = ["|", ("code", "=ilike", sub), ("name", "=ilike", sub)]
            if country:
                dom = [("country_id", "=", country.id)] + dom
            state = self.env["res.country.state"].search(dom, limit=1)
            if state:
                vals["state_id"] = state.id
                if not country:
                    vals["country_id"] = state.country_id.id
        return vals

    def _partner_vals(self, ctx, data, entity):
        name = data.get("DisplayName") or data.get("CompanyName") or \
            " ".join(filter(None, [data.get("GivenName"), data.get("FamilyName")])) or _("Unnamed")
        suffix = ctx.conn.vendor_suffix or ""
        if entity == "Vendor" and suffix and name.endswith(suffix):
            name = name[: -len(suffix)]
        vals = {
            "name": name,
            "is_company": bool(data.get("CompanyName")) or not data.get("GivenName"),
            "email": (data.get("PrimaryEmailAddr") or {}).get("Address") or False,
            "phone": (data.get("PrimaryPhone") or {}).get("FreeFormNumber")
            or (data.get("Mobile") or {}).get("FreeFormNumber") or False,
            "website": (data.get("WebAddr") or {}).get("URI") or False,
            "active": bool(data.get("Active", True)),
        }
        if data.get("Notes"):
            vals["comment"] = data["Notes"]
        if entity == "Vendor" and data.get("AcctNum"):
            vals["ref"] = data["AcctNum"]
        tax_id = data.get("PrimaryTaxIdentifier") or data.get("TaxIdentifier") or ""
        if tax_id and "X" not in tax_id.upper():  # QBO masks tax ids on read (XXXXX1234)
            vals["vat"] = tax_id
        vals.update(self._address_vals(data.get("BillAddr")))
        if not ctx.conn.share_partners:
            vals["company_id"] = ctx.conn.company_id.id
        term = self._ensure(ctx, "Term", data.get("SalesTermRef" if entity == "Customer" else "TermRef"))
        if term:
            key = "property_payment_term_id" if entity == "Customer" else "property_supplier_payment_term_id"
            vals[key] = term.id
        return vals

    def _match_partner(self, ctx, vals, entity):
        Partner = self.env["res.partner"].with_context(active_test=False)
        base = [("parent_id", "=", False)]
        if vals.get("email"):
            found = Partner.search(base + [("email", "=ilike", vals["email"])], limit=1)
            if found:
                return found
        if vals.get("vat"):
            found = Partner.search(base + [("vat", "=", vals["vat"])], limit=1)
            if found:
                return found
        found = Partner.search(base + [("name", "=ilike", vals["name"])], limit=1)
        return found or None

    def _import_partner(self, ctx, data, record, entity):
        vals = self._partner_vals(ctx, data, entity)
        rank = "customer_rank" if entity == "Customer" else "supplier_rank"
        if record is None and ctx.conn.match_existing:
            record = self._match_partner(ctx, vals, entity)
        parent_ref = data.get("ParentRef") if entity == "Customer" and data.get("Job") else None
        if parent_ref:
            parent = self._ensure(ctx, "Customer", parent_ref)
            if parent:
                vals.update(parent_id=parent.id, type="other", is_company=False)
        if record is None:
            vals[rank] = 1
            record = self.env["res.partner"].create(vals)
        else:
            if not record[rank]:
                vals[rank] = 1
            record.write(vals)
        ship = data.get("ShipAddr")
        if ship and ship.get("Line1") and ship.get("Line1") != (data.get("BillAddr") or {}).get("Line1"):
            ship_vals = self._address_vals(ship)
            child = record.child_ids.filtered(lambda c: c.type == "delivery")[:1]
            if child:
                child.write(ship_vals)
            else:
                self.env["res.partner"].create(dict(ship_vals, parent_id=record.id, type="delivery"))
        return record

    def _import_customer(self, ctx, data, record):
        return self._import_partner(ctx, data, record, "Customer")

    def _import_vendor(self, ctx, data, record):
        return self._import_partner(ctx, data, record, "Vendor")

    def _import_itemcategory(self, ctx, data, record):
        Categ = self.env["product.category"]
        parent = self._ensure(ctx, "ItemCategory", data.get("ParentRef")) if data.get("ParentRef") else None
        vals = {"name": data.get("Name"), "parent_id": parent.id if parent else False}
        if record is None:
            record = Categ.search([("name", "=", vals["name"]), ("parent_id", "=", vals["parent_id"])], limit=1) or None
        if record is None:
            record = Categ.create(vals)
        else:
            record.write(vals)
        return record

    def _import_item(self, ctx, data, record):
        if data.get("Type") == "Category":
            return self._import_itemcategory(ctx, data, record)
        Product = self.env["product.product"].with_context(active_test=False)
        qtype = data.get("Type")
        vals = {
            "name": data.get("Name"),
            "default_code": data.get("Sku") or False,
            "type": "service" if qtype == "Service" else "consu",
            "description_sale": data.get("Description") or False,
            "description_purchase": data.get("PurchaseDesc") or False,
            "list_price": float(data.get("UnitPrice") or 0.0),
            "standard_price": float(data.get("PurchaseCost") or 0.0),
            "active": bool(data.get("Active", True)),
            "sale_ok": True,
            "purchase_ok": bool(data.get("ExpenseAccountRef") or data.get("PurchaseCost")) or qtype == "Inventory",
        }
        if "is_storable" in Product._fields:
            vals["is_storable"] = qtype == "Inventory"
        categ = self._ensure(ctx, "ItemCategory", data.get("ParentRef")) if data.get("SubItem") else None
        if categ and categ._name == "product.category":
            vals["categ_id"] = categ.id
        income = self._account(ctx, data.get("IncomeAccountRef"))
        expense = self._account(ctx, data.get("ExpenseAccountRef"))
        if income:
            vals["property_account_income_id"] = income.id
        if expense:
            vals["property_account_expense_id"] = expense.id
        if not ctx.conn.is_us_company:
            if data.get("SalesTaxCodeRef"):
                vals["taxes_id"] = [Command.set(self._taxes(ctx, data["SalesTaxCodeRef"], "sale").ids)]
            if data.get("PurchaseTaxCodeRef"):
                vals["supplier_taxes_id"] = [Command.set(self._taxes(ctx, data["PurchaseTaxCodeRef"], "purchase").ids)]
        if record is None and ctx.conn.match_existing:
            if vals["default_code"]:
                record = Product.search([("default_code", "=", vals["default_code"])], limit=1) or None
            if record is None:
                record = Product.search([("name", "=ilike", vals["name"])], limit=1) or None
        is_new = record is None
        if is_new:
            record = Product.create(vals)
        else:
            record.write(vals)
        qty = float(data.get("QtyOnHand") or 0)
        if is_new and qty > 0 and vals.get("is_storable") and "stock.quant" in self.env:
            self._set_initial_stock(ctx, record, qty)
        return record

    def _set_initial_stock(self, ctx, product, qty):
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", ctx.conn.company_id.id)], limit=1)
        if not warehouse:
            return
        try:
            quant = self.env["stock.quant"].with_context(inventory_mode=True).create({
                "product_id": product.id, "location_id": warehouse.lot_stock_id.id, "inventory_quantity": qty})
            quant.action_apply_inventory()
        except Exception:  # pylint: disable=broad-except
            _logger.warning("Could not set initial stock for %s", product.display_name, exc_info=True)

    # ======================================================= document lines
    def _sales_lines(self, ctx, data, lines=None):
        out = []
        for line in lines if lines is not None else data.get("Line") or []:
            dtype = line.get("DetailType")
            amount = float(line.get("Amount") or 0.0)
            if dtype == "SalesItemLineDetail":
                d = line.get("SalesItemLineDetail") or {}
                product = self._product(ctx, d.get("ItemRef"))
                qty, price = self._qty_price(d.get("Qty"), d.get("UnitPrice"), amount)
                vals = {"product_id": product.id, "name": line.get("Description") or product.display_name,
                        "quantity": qty, "price_unit": price,
                        "tax_ids": [Command.set(self._taxes(ctx, d.get("TaxCodeRef"), "sale").ids)],
                        "analytic_distribution": self._analytic(ctx, d.get("ClassRef"), data.get("DepartmentRef"))}
                out.append(vals)
            elif dtype == "GroupLineDetail":
                out += self._sales_lines(ctx, data, (line.get("GroupLineDetail") or {}).get("Line") or [])
            elif dtype == "DescriptionOnly" and line.get("Description"):
                out.append({"display_type": "line_note", "name": line["Description"]})
            elif dtype == "DiscountLineDetail" and amount:
                d = line.get("DiscountLineDetail") or {}
                account = self._account(ctx, d.get("DiscountAccountRef")) or ctx.conn.discount_account_id
                taxed = [l for l in out if l.get("tax_ids") and l.get("price_unit") is not None]
                tax_sets = {tuple(sorted(l["tax_ids"][0][2])) for l in taxed if l["tax_ids"][0][2]}
                vals = {"name": line.get("Description") or _("Discount"), "quantity": 1, "price_unit": -amount,
                        "tax_ids": [Command.set(list(tax_sets.pop()))] if len(tax_sets) == 1 else [Command.clear()]}
                if account:
                    vals["account_id"] = account.id
                out.append(vals)
        return out

    def _expense_lines(self, ctx, data, lines=None):
        out = []
        for line in lines if lines is not None else data.get("Line") or []:
            dtype = line.get("DetailType")
            amount = float(line.get("Amount") or 0.0)
            if dtype == "ItemBasedExpenseLineDetail":
                d = line.get("ItemBasedExpenseLineDetail") or {}
                product = self._product(ctx, d.get("ItemRef"))
                qty, price = self._qty_price(d.get("Qty"), d.get("UnitPrice"), amount)
                out.append({"product_id": product.id, "name": line.get("Description") or product.display_name,
                            "quantity": qty, "price_unit": price,
                            "tax_ids": [Command.set(self._taxes(ctx, d.get("TaxCodeRef"), "purchase").ids)],
                            "analytic_distribution": self._analytic(ctx, d.get("ClassRef"), data.get("DepartmentRef"))})
            elif dtype == "AccountBasedExpenseLineDetail":
                d = line.get("AccountBasedExpenseLineDetail") or {}
                account = self._account(ctx, d.get("AccountRef")) or ctx.conn.expense_account_id
                vals = {"name": line.get("Description") or (account.name if account else "/"), "quantity": 1,
                        "price_unit": amount,
                        "tax_ids": [Command.set(self._taxes(ctx, d.get("TaxCodeRef"), "purchase").ids)],
                        "analytic_distribution": self._analytic(ctx, d.get("ClassRef"), data.get("DepartmentRef"))}
                if account:
                    vals["account_id"] = account.id
                out.append(vals)
            elif dtype == "GroupLineDetail":
                out += self._expense_lines(ctx, data, (line.get("GroupLineDetail") or {}).get("Line") or [])
            elif dtype == "DescriptionOnly" and line.get("Description"):
                out.append({"display_type": "line_note", "name": line["Description"]})
        return out

    @staticmethod
    def _qty_price(qty, price, amount):
        qty = float(qty) if qty not in (None, "") else 0.0
        if not qty:
            return 1.0, amount
        price = float(price) if price not in (None, "") else None
        if price is None or abs(qty * price - amount) > 0.01:
            price = amount / qty
        return qty, price

    def _us_tax_line(self, ctx, data, sign=1):
        total_tax = float((data.get("TxnTaxDetail") or {}).get("TotalTax") or 0.0)
        if not ctx.conn.is_us_company or float_is_zero(total_tax, precision_digits=2):
            return []
        account = ctx.conn.sales_tax_account_id or self._default_sales_tax_account(ctx)
        return [{"name": _("Sales Tax (QuickBooks)"), "quantity": 1, "price_unit": total_tax * sign,
                 "account_id": account.id, "tax_ids": [Command.clear()]}]

    def _default_sales_tax_account(self, ctx):
        Account = self.env["account.account"]
        company = ctx.conn.company_id
        account = Account.search([("company_ids", "in", company.ids), ("code", "=", "QBTAX")], limit=1)
        if not account:
            account = Account.create({"name": "Sales Tax Payable (QuickBooks)", "code": "QBTAX",
                                      "account_type": "liability_current", "company_ids": [Command.set(company.ids)]})
        ctx.conn.sudo().sales_tax_account_id = account
        return account

    # ============================================================ invoices
    def _is_voided(self, data):
        note = (data.get("PrivateNote") or "").lower()
        return float(data.get("TotalAmt") or 0) == 0 and ("voided" in note or "void" in note)

    def _import_move(self, ctx, data, record, entity):
        if ctx.job and ctx.job.operation == "Void" or (record is not None and self._is_voided(data)):
            return self._apply_void(ctx, entity, record, data)
        txn_date = fields.Date.to_date(data.get("TxnDate"))
        if not record and ctx.conn.import_start_date and txn_date and txn_date < ctx.conn.import_start_date \
                and not self.env.context.get("qb_force"):
            raise QbSkip(_("Transaction dated before the import start date."))
        move_type = MOVE_TYPE[entity]
        sale_side = move_type.startswith("out_")
        partner = self._partner(ctx, data.get("CustomerRef" if sale_side else "VendorRef"),
                                "Customer" if sale_side else "Vendor")
        currency = self._currency(ctx, data.get("CurrencyRef"))
        journal = ctx.conn.sale_journal_id if sale_side else ctx.conn.purchase_journal_id
        lines = self._sales_lines(ctx, data) if sale_side else self._expense_lines(ctx, data)
        lines += self._us_tax_line(ctx, data)
        term = self._ensure(ctx, "Term", data.get("SalesTermRef"))
        vals = {
            "move_type": move_type,
            "partner_id": partner.id,
            "invoice_date": txn_date,
            "date": txn_date,
            "currency_id": currency.id,
            "journal_id": journal.id,
            "narration": (data.get("CustomerMemo") or {}).get("value") or data.get("PrivateNote") or False,
        }
        if term:
            vals["invoice_payment_term_id"] = term.id
        elif data.get("DueDate"):
            vals["invoice_payment_term_id"] = False
            vals["invoice_date_due"] = fields.Date.to_date(data["DueDate"])
        if sale_side:
            vals["payment_reference"] = data.get("DocNumber") or False
        else:
            vals["ref"] = data.get("DocNumber") or False
        reconciled = self.env["account.move.line"]
        if record is None:
            vals["invoice_line_ids"] = [Command.create(l) for l in lines]
            record = self.env["account.move"].with_company(ctx.conn.company_id).create(vals)
        else:
            if record.state == "posted":
                reconciled = self._reconciled_counterparts(record)
                record.button_draft()
            elif record.state == "cancel":
                record.button_draft()
            vals["invoice_line_ids"] = [Command.clear()] + [Command.create(l) for l in lines]
            record.write(vals)
        if data.get("ExchangeRate") and currency != ctx.conn.company_id.currency_id:
            self._apply_rate(record, float(data["ExchangeRate"]))
        if sale_side and ctx.conn.keep_qb_numbers and data.get("DocNumber"):
            self._set_number(record, data["DocNumber"])
        if ctx.conn.auto_post and record.invoice_line_ids:
            record.action_post()
            if reconciled:
                self._reconcile(record, reconciled)
        self._check_total(ctx, record, data)
        if ctx.conn.fetch_pdf and entity in ("Invoice", "CreditMemo", "SalesReceipt"):
            self._attach_pdf(ctx, entity, data["Id"], record)
        return record

    def _apply_rate(self, move, rate):
        """Force the QuickBooks exchange rate (home currency per unit of foreign)."""
        if rate and "invoice_currency_rate" in move._fields:
            move.invoice_currency_rate = 1.0 / rate

    def _set_number(self, move, number):
        number = str(number)
        dup = self.env["account.move"].search_count([("journal_id", "=", move.journal_id.id), ("name", "=", number),
                                                     ("id", "!=", move.id)])
        if not dup and move.name != number:
            try:
                with self.env.cr.savepoint():
                    move.name = number
            except Exception:  # pylint: disable=broad-except
                _logger.info("Could not keep QuickBooks number %s", number, exc_info=True)

    def _check_total(self, ctx, move, data):
        qb_total = float(data.get("TotalAmt") or 0.0)
        if move.move_type == "entry" or float_compare(abs(move.amount_total), abs(qb_total),
                                                      precision_rounding=move.currency_id.rounding) == 0:
            return
        self.env["qb.sync.log"]._log(
            ctx.conn, _("Total mismatch on %(doc)s: Odoo %(odoo)s vs QuickBooks %(qb)s (tax rounding or tax "
                        "mapping). Run the Integrity Check for details.", doc=move.display_name,
                        odoo=move.amount_total, qb=qb_total),
            level="warning", direction="import", job=ctx.job, record=move)

    def _reconciled_counterparts(self, move):
        lines = move.line_ids.filtered(lambda l: l.account_type in ("asset_receivable", "liability_payable"))
        counterparts = lines.matched_debit_ids.debit_move_id | lines.matched_credit_ids.credit_move_id
        return counterparts - lines

    def _reconcile(self, move, counterparts):
        lines = move.line_ids.filtered(lambda l: l.account_type in ("asset_receivable", "liability_payable")
                                       and not l.reconciled)
        for cp in counterparts.filtered(lambda l: not l.reconciled):
            target = lines.filtered(lambda l: l.account_id == cp.account_id and not l.reconciled)
            if target:
                (target + cp).reconcile()

    def _attach_pdf(self, ctx, entity, qb_id, record):
        try:
            content = ctx.client.pdf(entity, qb_id)
        except QBOError:
            _logger.info("PDF download failed", exc_info=True)
            return
        self.env["ir.attachment"].create({
            "name": "QuickBooks-%s-%s.pdf" % (entity, qb_id), "type": "binary",
            "datas": base64.b64encode(content), "res_model": record._name, "res_id": record.id,
            "mimetype": "application/pdf"})

    def _apply_void(self, ctx, entity, record, data):
        if record is None:
            raise QbSkip(_("Voided document was never imported."))
        if ctx.conn.void_policy == "cancel":
            self._cancel_record(record)
        ctx.voided = True
        self.env["qb.sync.log"]._log(ctx.conn, _("%s voided in QuickBooks.", record.display_name), level="warning",
                                     direction="import", job=ctx.job, record=record)
        return record

    def _import_invoice(self, ctx, data, record):
        return self._import_move(ctx, data, record, "Invoice")

    def _import_creditmemo(self, ctx, data, record):
        return self._import_move(ctx, data, record, "CreditMemo")

    def _import_bill(self, ctx, data, record):
        return self._import_move(ctx, data, record, "Bill")

    def _import_vendorcredit(self, ctx, data, record):
        return self._import_move(ctx, data, record, "VendorCredit")

    def _import_salesreceipt(self, ctx, data, record):
        is_new = record is None
        move = self._import_move(ctx, data, record, "SalesReceipt")
        if is_new and move.state == "posted" and move.amount_residual:
            journal = self._journal_for(ctx, data.get("DepositToAccountRef"), data.get("PaymentMethodRef"))
            outstanding = self._account(ctx, data.get("DepositToAccountRef"))
            payment = self._new_payment(ctx, move.partner_id, move.amount_residual, move.currency_id,
                                        move.invoice_date, journal, "inbound", "customer",
                                        memo=data.get("DocNumber"), outstanding=outstanding)
            self._reconcile_payment(payment, [(move, move.amount_residual)])
        return move

    def _import_journalentry(self, ctx, data, record):
        txn_date = fields.Date.to_date(data.get("TxnDate"))
        if not record and ctx.conn.import_start_date and txn_date and txn_date < ctx.conn.import_start_date \
                and not self.env.context.get("qb_force"):
            raise QbSkip(_("Transaction dated before the import start date."))
        currency = self._currency(ctx, data.get("CurrencyRef"))
        lines = []
        rate = float(data.get("ExchangeRate") or 1.0)
        for line in data.get("Line") or []:
            if line.get("DetailType") != "JournalEntryLineDetail":
                continue
            d = line["JournalEntryLineDetail"]
            amount = float(line.get("Amount") or 0.0)
            sign = 1 if d.get("PostingType") == "Debit" else -1
            account = self._account(ctx, d.get("AccountRef"), required=True)
            vals = {"name": line.get("Description") or data.get("DocNumber") or "/", "account_id": account.id,
                    "analytic_distribution": self._analytic(ctx, d.get("ClassRef"), d.get("DepartmentRef"))}
            if currency != ctx.conn.company_id.currency_id:
                vals.update(currency_id=currency.id, amount_currency=sign * amount,
                            balance=ctx.conn.company_id.currency_id.round(sign * amount * rate))
            else:
                vals["balance"] = sign * amount
            ent = d.get("Entity") or {}
            if ent.get("EntityRef"):
                etype = ent.get("Type") or "Customer"
                if etype in ("Customer", "Vendor"):
                    vals["partner_id"] = self._partner(ctx, ent["EntityRef"], etype).id
            lines.append(vals)
        vals = {"move_type": "entry", "date": txn_date, "ref": data.get("DocNumber") or False,
                "journal_id": ctx.conn.misc_journal_id.id, "currency_id": currency.id,
                "narration": data.get("PrivateNote") or False}
        if record is None:
            vals["line_ids"] = [Command.create(l) for l in lines]
            record = self.env["account.move"].create(vals)
        else:
            if record.state != "draft":
                record.button_draft()
            vals["line_ids"] = [Command.clear()] + [Command.create(l) for l in lines]
            record.write(vals)
        if ctx.conn.auto_post:
            record.action_post()
        return record

    # ============================================================ payments
    def _new_payment(self, ctx, partner, amount, currency, date, journal, ptype, partner_type, memo=None,
                     outstanding=None):
        vals = {"payment_type": ptype, "partner_type": partner_type, "partner_id": partner.id,
                "amount": amount, "currency_id": currency.id, "date": date, "journal_id": journal.id,
                "memo": memo or False, "company_id": ctx.conn.company_id.id}
        use_deposit = outstanding and outstanding.account_type not in ("asset_receivable", "liability_payable")
        Payment = self.env["account.payment"] if use_deposit else \
            self.env["account.payment"].with_context(force_payment_move=True)
        payment = Payment.create(vals)
        if use_deposit:
            # QuickBooks "Deposit to" account: book straight on it (written after
            # create because Odoo recomputes it from the payment method line)
            payment.outstanding_account_id = outstanding
        elif not payment.outstanding_account_id:
            payment.outstanding_account_id = payment._get_outstanding_account(payment.payment_type)
        payment.action_post()
        return payment

    def _reconcile_payment(self, payment, allocations):
        """allocations: list of (move, amount) in document currency."""
        pay_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_type in ("asset_receivable", "liability_payable") and not l.reconciled)
        for move, _amount in allocations:
            if move.state != "posted":
                continue
            inv_lines = move.line_ids.filtered(
                lambda l: l.account_type in ("asset_receivable", "liability_payable") and not l.reconciled)
            for pl in pay_lines.filtered(lambda l: not l.reconciled):
                target = inv_lines.filtered(lambda l: l.account_id == pl.account_id)
                if target:
                    (pl + target).reconcile()

    def _linked_moves(self, ctx, data, txn_type, entity):
        allocations = []
        for line in data.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                if linked.get("TxnType") != txn_type:
                    continue
                move = self._ensure(ctx, entity, linked.get("TxnId"))
                if move:
                    allocations.append((move, float(line.get("Amount") or 0.0)))
        return allocations

    def _import_payment_generic(self, ctx, data, record, entity):
        is_customer = entity == "Payment"
        txn_date = fields.Date.to_date(data.get("TxnDate"))
        if not record and ctx.conn.import_start_date and txn_date and txn_date < ctx.conn.import_start_date \
                and not self.env.context.get("qb_force"):
            raise QbSkip(_("Transaction dated before the import start date."))
        partner = self._partner(ctx, data.get("CustomerRef" if is_customer else "VendorRef"),
                                "Customer" if is_customer else "Vendor")
        currency = self._currency(ctx, data.get("CurrencyRef"))
        amount = float(data.get("TotalAmt") or 0.0)
        if is_customer:
            account_ref = data.get("DepositToAccountRef")
            allocations = self._linked_moves(ctx, data, "Invoice", "Invoice")
        else:
            pay_detail = data.get("CheckPayment") or data.get("CreditCardPayment") or {}
            account_ref = pay_detail.get("BankAccountRef") or pay_detail.get("CCAccountRef")
            allocations = self._linked_moves(ctx, data, "Bill", "Bill")
        journal = self._journal_for(ctx, account_ref, data.get("PaymentMethodRef"))
        if not journal:
            raise UserError(_("No payment journal found. Set a default payment journal on the connection."))
        outstanding = self._account(ctx, account_ref)
        if self._is_voided(data) or (ctx.job and ctx.job.operation == "Void"):
            if record:
                return self._apply_void(ctx, entity, record, data)
            raise QbSkip(_("Voided payment."))
        if float_is_zero(amount, precision_digits=2):
            raise QbSkip(_("Zero-amount payment (credit application only)."))
        memo = data.get("PaymentRefNum") or data.get("DocNumber")
        if record is not None:
            same = (float_compare(record.amount, amount, precision_digits=2) == 0 and record.date == txn_date
                    and record.journal_id == journal and record.partner_id == partner)
            if same:
                self._reconcile_payment(record, allocations)
                return record
            record.action_draft()
            record.action_cancel()
        payment = self._new_payment(ctx, partner, amount, currency, txn_date, journal,
                                    "inbound" if is_customer else "outbound",
                                    "customer" if is_customer else "supplier", memo=memo, outstanding=outstanding)
        self._reconcile_payment(payment, allocations)
        return payment

    def _import_payment(self, ctx, data, record):
        return self._import_payment_generic(ctx, data, record, "Payment")

    def _import_billpayment(self, ctx, data, record):
        return self._import_payment_generic(ctx, data, record, "BillPayment")

    # ============================================================== orders
    def _order_lines(self, ctx, data, purchase=False):
        lines = self._expense_lines(ctx, data) if purchase else self._sales_lines(ctx, data)
        out = []
        for l in lines:
            if l.get("display_type"):
                out.append({"display_type": "line_note", "name": l["name"]})
                continue
            product = self.env["product.product"].browse(l.get("product_id")) if l.get("product_id") \
                else self._misc_product(ctx)
            vals = {"product_id": product.id, "name": l["name"], "price_unit": l["price_unit"],
                    "tax_ids": l.get("tax_ids") or [Command.clear()]}
            if purchase:
                vals["product_qty"] = l.get("quantity", 1)
                vals["date_planned"] = fields.Datetime.to_datetime(data.get("DueDate") or data.get("TxnDate"))
            else:
                vals["product_uom_qty"] = l.get("quantity", 1)
            if l.get("analytic_distribution"):
                vals["analytic_distribution"] = l["analytic_distribution"]
            out.append(vals)
        return out

    def _import_estimate(self, ctx, data, record):
        partner = self._partner(ctx, data.get("CustomerRef"), "Customer")
        vals = {"partner_id": partner.id, "company_id": ctx.conn.company_id.id,
                "date_order": fields.Datetime.to_datetime(data.get("TxnDate")),
                "validity_date": fields.Date.to_date(data.get("ExpirationDate")) if data.get("ExpirationDate") else False,
                "client_order_ref": data.get("DocNumber") or False,
                "note": (data.get("CustomerMemo") or {}).get("value") or False}
        status = data.get("TxnStatus")
        if record is None or record.state in ("draft", "sent"):
            vals["order_line"] = ([Command.clear()] if record is not None else []) + \
                [Command.create(l) for l in self._order_lines(ctx, data)]
            if record is None:
                record = self.env["sale.order"].create(vals)
            else:
                record.write(vals)
        if status in ("Accepted", "Closed", "Converted") and record.state in ("draft", "sent") \
                and ctx.conn.confirm_accepted_estimates:
            record.action_confirm()
        elif status == "Rejected" and record.state != "cancel":
            record._action_cancel()
        return record

    def _import_purchaseorder(self, ctx, data, record):
        partner = self._partner(ctx, data.get("VendorRef"), "Vendor")
        currency = self._currency(ctx, data.get("CurrencyRef"))
        vals = {"partner_id": partner.id, "company_id": ctx.conn.company_id.id, "currency_id": currency.id,
                "date_order": fields.Datetime.to_datetime(data.get("TxnDate")),
                "partner_ref": data.get("DocNumber") or False,
                "note": data.get("Memo") or data.get("PrivateNote") or False}
        if record is None or record.state in ("draft", "sent"):
            vals["order_line"] = ([Command.clear()] if record is not None else []) + \
                [Command.create(l) for l in self._order_lines(ctx, data, purchase=True)]
            if record is None:
                record = self.env["purchase.order"].create(vals)
            else:
                record.write(vals)
        status = data.get("POStatus")
        if status in ("Open", "Closed") and record.state in ("draft", "sent") and ctx.conn.confirm_open_pos:
            record.button_confirm()
        if status == "Closed" and record.state == "purchase" and "locked" in record._fields:
            record.locked = True
        return record

    # ===================================================== deletes / merges
    def _cancel_record(self, record):
        model = record._name
        if model == "account.move":
            if record.state == "posted":
                record.button_draft()
            if record.state == "draft":
                record.button_cancel()
        elif model == "account.payment":
            if record.state not in ("draft", "canceled"):
                record.action_draft()
            record.action_cancel()
        elif model == "sale.order":
            if record.state != "cancel":
                record._action_cancel()
        elif model == "purchase.order":
            if record.state != "cancel":
                record.button_cancel()
        elif "active" in record._fields:
            record.active = False

    def _handle_deleted(self, ctx, entity, qb_id):
        mapping = self.env["qb.mapping"]._find(ctx.conn, entity, qb_id)
        if not mapping:
            raise QbSkip(_("Deleted record was never synced."))
        record = self.env[mapping.res_model].browse(mapping.res_id).exists() if mapping.res_model else None
        if record and ctx.conn.deletion_policy == "mirror":
            self._cancel_record(record)
        mapping.state = "deleted_qb"
        self.env["qb.sync.log"]._log(ctx.conn, _("%(entity)s %(id)s deleted in QuickBooks%(what)s.", entity=entity,
                                                 id=qb_id, what=_(" and was archived/cancelled in Odoo")
                                                 if record and ctx.conn.deletion_policy == "mirror" else ""),
                                     level="warning", direction="import", job=ctx.job, record=record)
        return record

    def _handle_merge(self, ctx, entity, old_id, new_id):
        Mapping = self.env["qb.mapping"]
        old = Mapping._find(ctx.conn, entity, old_id)
        new = Mapping._find(ctx.conn, entity, new_id)
        if old and not new:
            old.qb_id = str(new_id)
            return old.record_ref
        if old:
            return self._handle_deleted(ctx, entity, old_id)
        raise QbSkip(_("Merged record was never synced."))

    @api.model
    def _import_one(self, connection, entity, qb_id):
        """Synchronous helper used by buttons."""
        job = connection._enqueue("import", entity, qb_id=qb_id, priority_boost=True)
        job._run_one()
        return job
