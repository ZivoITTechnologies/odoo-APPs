# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
"""In-memory QuickBooks Online API simulator used by the test-suite.

It implements the subset of the Intuit v3 REST API the connector relies on
(OAuth token endpoint, CRUD with SyncToken optimistic locking, query with
WHERE / paging, CDC with deletions, void, delete, PDF, send) so every flow
is exercised end-to-end through the real HTTP client code."""
import copy
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..lib import qbo_client


class FakeResponse:
    def __init__(self, status, body=None, content=None, headers=None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else json.dumps(body or {}).encode()
        self.text = self.content.decode(errors="ignore") if isinstance(self.content, bytes) else str(self.content)
        self.headers = headers or {"intuit_tid": "test-tid"}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _fault(code, message, status=400):
    return FakeResponse(status, {"Fault": {"Error": [{"Message": message, "Detail": message, "code": str(code)}],
                                           "type": "ValidationFault"}})


class FakeQBO:
    TXN = ("Invoice", "CreditMemo", "SalesReceipt", "Bill", "VendorCredit", "Payment", "BillPayment",
           "Estimate", "PurchaseOrder", "JournalEntry")

    def __init__(self, country="CA"):
        self.db = {}
        self.next_id = 1000
        self.calls = []
        self.failures = []           # list of (predicate(method, path, params), FakeResponse)
        self.cdc_feed = {}
        self.country = country
        self.refresh_count = 0
        self.revoked = False
        self.pdf_bytes = b"%PDF-1.4 fake"

    # ------------------------------------------------------------- helpers
    def add(self, entity, obj):
        obj = copy.deepcopy(obj)
        obj.setdefault("Id", str(self._new_id()))
        obj.setdefault("SyncToken", "0")
        obj.setdefault("MetaData", {"LastUpdatedTime": self._now()})
        if entity in ("Invoice", "CreditMemo", "Bill", "VendorCredit", "SalesReceipt", "Estimate", "PurchaseOrder"):
            obj.setdefault("TotalAmt", self._total(obj))
            if entity in ("Invoice", "Bill"):
                obj.setdefault("Balance", obj["TotalAmt"])
        self.db.setdefault(entity, {})[obj["Id"]] = obj
        return obj

    def get(self, entity, qb_id):
        return self.db.get(entity, {}).get(str(qb_id))

    def all(self, entity):
        return list(self.db.get(entity, {}).values())

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-00:00")

    @staticmethod
    def _total(obj):
        lines = obj.get("Line") or []
        total = 0.0
        for l in lines:
            if l.get("DetailType") in ("SubTotalLineDetail", "DescriptionOnly"):
                continue
            if l.get("DetailType") == "DiscountLineDetail":
                total -= float(l.get("Amount") or 0)
            elif l.get("DetailType") != "JournalEntryLineDetail":
                total += float(l.get("Amount") or 0)
        total += float((obj.get("TxnTaxDetail") or {}).get("TotalTax") or 0)
        return round(total, 2)

    def fail_next(self, predicate, response):
        self.failures.append((predicate, response))

    # ------------------------------------------------------------ transport
    def session(self):
        fake = self

        class Session:
            def post(self, url, data=None, json=None, headers=None, timeout=None, **kw):
                return fake.handle("POST", url, {}, data if data is not None else json, headers or {})

            def request(self, method, url, params=None, data=None, headers=None, timeout=None, **kw):
                return fake.handle(method, url, params or {}, data, headers or {})
        return Session()

    def handle(self, method, url, params, data, headers):
        self.calls.append((method, url, dict(params), data))
        for i, (pred, resp) in enumerate(self.failures):
            if pred(method, url, params):
                self.failures.pop(i)
                return resp
        if url == qbo_client.TOKEN_URL:
            return self._token(data)
        if url == qbo_client.REVOKE_URL:
            self.revoked = True
            return FakeResponse(200, {})
        if not (headers.get("Authorization") or "").startswith("Bearer at-"):
            return FakeResponse(401, {"fault": {"error": [{"message": "AuthenticationFailed", "code": "3200"}]}})
        path = re.sub(r"^https://[^/]+/v3/company/[^/]+/", "", url)
        payload = json.loads(data) if isinstance(data, (str, bytes)) and data else None
        if path.startswith("companyinfo"):
            return FakeResponse(200, {"CompanyInfo": {"CompanyName": "Fake Co %s" % self.country,
                                                      "Country": self.country}})
        if path == "preferences":
            return FakeResponse(200, {"Preferences": {"CurrencyPrefs": {"HomeCurrency": {"value": "USD"},
                                                                        "MultiCurrencyEnabled": True}}})
        if path == "query":
            return self._query(params["query"])
        if path == "cdc":
            return self._cdc(params)
        if path == "batch":
            return FakeResponse(200, {"BatchItemResponse": []})
        m = re.match(r"^(\w+)/(\w+)/pdf$", path)
        if m:
            return FakeResponse(200, None, content=self.pdf_bytes, headers={"Content-Type": "application/pdf"})
        m = re.match(r"^(\w+)/(\w+)/send$", path)
        if m:
            entity = self._entity(m.group(1))
            obj = self.get(entity, m.group(2))
            obj["EmailStatus"] = "EmailSent"
            return FakeResponse(200, {entity: obj})
        m = re.match(r"^(\w+)/(\w+)$", path)
        if m and method == "GET":
            entity = self._entity(m.group(1))
            obj = self.get(entity, m.group(2))
            if not obj:
                return _fault(610, "Object Not Found")
            return FakeResponse(200, {entity: copy.deepcopy(obj)})
        m = re.match(r"^(\w+)$", path)
        if m and method == "POST":
            return self._write(self._entity(m.group(1)), params, payload)
        return _fault(400, "Unsupported fake path %s" % path)

    def _entity(self, lower):
        names = ["Account", "TaxCode", "TaxRate", "Term", "PaymentMethod", "Class", "Department", "Customer",
                 "Vendor", "Item", "Estimate", "PurchaseOrder", "Invoice", "CreditMemo", "SalesReceipt", "Bill",
                 "VendorCredit", "JournalEntry", "Payment", "BillPayment"]
        return next(n for n in names if n.lower() == lower.lower())

    def _token(self, data):
        form = parse_qs(data) if isinstance(data, str) else {k: [v] for k, v in (data or {}).items()}
        grant = form.get("grant_type", [""])[0]
        if grant == "refresh_token" and not form.get("refresh_token", [""])[0].startswith("rt-"):
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "Token invalid"})
        self.refresh_count += 1
        return FakeResponse(200, {"access_token": "at-%d" % self.refresh_count,
                                  "refresh_token": "rt-%d" % self.refresh_count, "expires_in": 3600,
                                  "x_refresh_token_expires_in": 8726400, "token_type": "bearer"})

    # ------------------------------------------------------------- query
    def _query(self, stmt):
        m = re.match(r"SELECT (\*|COUNT\(\*\)) FROM (\w+)(?: WHERE (.*?))?(?: ORDERBY .*?)?"
                     r"(?: STARTPOSITION (\d+))?(?: MAXRESULTS (\d+))?$", stmt.strip(), re.I)
        if not m:
            return _fault(4000, "QueryParserError: %s" % stmt)
        entity = m.group(2)
        where = m.group(3)
        start = int(m.group(4) or 1)
        size = int(m.group(5) or 100)
        records = self.all(entity)
        if where:
            for cond in re.split(r"\s+AND\s+", where):
                cond = cond.strip().strip("()")
                cm = re.match(r"(\w+)\s*(=|>=|<=)\s*'((?:[^'\\]|\\.)*)'", cond)
                if not cm:
                    continue
                field, op, value = cm.group(1), cm.group(2), cm.group(3).replace("\\'", "'")

                def val(r, f=field):
                    v = r.get(f)
                    if f == "PrimaryEmailAddr":
                        v = (v or {}).get("Address")
                    return "" if v is None else str(v)
                if op == "=":
                    records = [r for r in records if val(r) == value]
                elif op == ">=":
                    records = [r for r in records if val(r) >= value]
                else:
                    records = [r for r in records if val(r) <= value]
        if m.group(1).upper().startswith("COUNT"):
            return FakeResponse(200, {"QueryResponse": {"totalCount": len(records)}})
        page = records[start - 1:start - 1 + size]
        return FakeResponse(200, {"QueryResponse": {entity: copy.deepcopy(page)} if page else {}})

    def _cdc(self, params):
        out = []
        for entity in params["entities"].split(","):
            if entity in self.cdc_feed:
                out.append({entity: self.cdc_feed[entity]})
        return FakeResponse(200, {"CDCResponse": [{"QueryResponse": out}]})

    # ------------------------------------------------------------- writes
    def _write(self, entity, params, payload):
        op = params.get("operation")
        if op in ("delete", "void") or params.get("include") == "void":
            obj = self.get(entity, payload["Id"])
            if not obj:
                return _fault(610, "Object Not Found")
            if str(obj["SyncToken"]) != str(payload["SyncToken"]):
                return _fault(5010, "Stale Object Error")
            if op == "delete":
                del self.db[entity][obj["Id"]]
                return FakeResponse(200, {entity: {"Id": obj["Id"], "status": "Deleted"}})
            obj.update(TotalAmt=0, Balance=0, PrivateNote="Voided", SyncToken=str(int(obj["SyncToken"]) + 1))
            return FakeResponse(200, {entity: copy.deepcopy(obj)})
        if payload.get("Id"):
            obj = self.get(entity, payload["Id"])
            if not obj:
                return _fault(610, "Object Not Found")
            if str(obj["SyncToken"]) != str(payload.get("SyncToken")):
                return _fault(5010, "Stale Object Error: You and another user were working on the same thing.")
            if "DisplayName" in payload and entity in ("Customer", "Vendor"):
                if self._dup_name(payload["DisplayName"], exclude=obj["Id"]):
                    return _fault(6240, "Duplicate Name Exists Error")
            data = {k: v for k, v in payload.items() if k not in ("sparse",)}
            obj.update(data)
            obj["SyncToken"] = str(int(obj["SyncToken"]) + 1)
            obj["MetaData"] = {"LastUpdatedTime": self._now()}
            if "Line" in payload and entity in self.TXN and entity not in ("Payment", "BillPayment", "JournalEntry"):
                obj["TotalAmt"] = self._total(obj)
            return FakeResponse(200, {entity: copy.deepcopy(obj)})
        if entity in ("Customer", "Vendor") and self._dup_name(payload.get("DisplayName")):
            return _fault(6240, "Duplicate Name Exists Error")
        if entity in ("Item", "Account", "Term", "Class", "Department"):
            if any(r.get("Name") == payload.get("Name") and (entity != "Item" or r.get("Type") != "Category"
                                                               or payload.get("Type") == "Category")
                   for r in self.all(entity)):
                return _fault(6240, "Duplicate Name Exists Error")
        if entity in ("Invoice", "Bill") and not payload.get("Line"):
            return _fault(6000, "A business validation error has occurred: Line is required")
        obj = self.add(entity, payload)
        if entity == "Invoice":
            obj["InvoiceLink"] = "https://connect.intuit.com/t/fake-%s" % obj["Id"]
        return FakeResponse(200, {entity: copy.deepcopy(obj)})

    def _dup_name(self, name, exclude=None):
        for ent in ("Customer", "Vendor"):
            for r in self.all(ent):
                if r.get("DisplayName") == name and r["Id"] != exclude:
                    return True
        return False


class QbCommon(AccountTestInvoicingCommon):
    country = "CA"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.fake = FakeQBO(country=cls.country)
        cls._patches = [
            patch.object(qbo_client, "SESSION_FACTORY", lambda: cls.fake.session()),
            patch.object(qbo_client, "_sleep", lambda s: None),
            patch.object(qbo_client._RateLimiter, "enabled", False),
        ]
        for p in cls._patches:
            p.start()
        cls.addClassCleanup(lambda: [p.stop() for p in cls._patches])
        cls.company = cls.env.company
        for xmlid in ("sales_team.group_sale_manager", "purchase.group_purchase_manager"):
            group = cls.env.ref(xmlid, raise_if_not_found=False)
            if group:
                cls.env.user.group_ids = [(4, group.id)]
        cls.conn = cls.env["qb.connection"].create({
            "name": "Test QBO", "company_id": cls.company.id, "environment": "sandbox",
            "client_id": "cid", "client_secret": "csecret", "webhook_verifier_token": "verifier-123",
            "import_start_date": "2020-01-01", "export_start_date": "2020-01-01",
            "auto_export_on_post": False, "income_account_id": cls.company_data["default_account_revenue"].id,
            "expense_account_id": cls.company_data["default_account_expense"].id,
        })
        cls.conn._save_tokens({"access_token": "at-0", "refresh_token": "rt-0", "expires_in": 3600})
        cls.conn.realm_id = "9130000000000"
        cls.conn._after_connect()
        cls.Mapping = cls.env["qb.mapping"]
        cls.Job = cls.env["qb.sync.job"]

    def setUp(self):
        super().setUp()
        cls = type(self)
        # isolate the fake QuickBooks company between tests (Odoo rolls back, so must the fake)
        if "_fake_snapshot" not in cls.__dict__:
            cls._fake_snapshot = copy.deepcopy(self.fake.db)
        self.fake.db = copy.deepcopy(cls._fake_snapshot)
        self.fake.failures = []
        self.fake.cdc_feed = {}

    # -------------------------------------------------------------- helpers
    def run_queue(self):
        total = 0
        while True:
            n = self.conn._process_queue()
            total += n
            if not n:
                return total

    def import_obj(self, entity, obj):
        """Put obj in fake QBO and import it through a real job."""
        if "Id" not in obj or not self.fake.get(entity if entity != "ItemCategory" else "Item", obj["Id"]):
            obj = self.fake.add("Item" if entity == "ItemCategory" else entity, obj)
        job = self.conn._enqueue("import", entity, qb_id=obj["Id"], payload=obj)
        job._run_one()
        return job

    def assertJobDone(self, job):
        self.assertIn(job.state, ("done", "skipped"), "Job failed: %s\n%s" % (job.error, job.error_detail))

    def mapped(self, entity, qb_id):
        return self.Mapping._find_record(self.conn, entity, qb_id)

    def seed_masters(self):
        """A small, realistic QuickBooks company."""
        f = self.fake
        self.qb_income = f.add("Account", {"Name": "Sales of Product Income", "AccountType": "Income",
                                           "AccountSubType": "SalesOfProductIncome", "AcctNum": "4100"})
        self.qb_expense = f.add("Account", {"Name": "Supplies", "AccountType": "Expense", "AcctNum": "6100"})
        self.qb_bank = f.add("Account", {"Name": "Checking", "AccountType": "Bank", "AcctNum": "1010"})
        self.qb_cogs = f.add("Account", {"Name": "Cost of Goods Sold", "AccountType": "Cost of Goods Sold"})
        self.qb_ap = f.add("Account", {"Name": "Accounts Payable (A/P)", "AccountType": "Accounts Payable"})
        self.qb_inv_asset = f.add("Account", {"Name": "Inventory Asset", "AccountType": "Other Current Asset",
                                              "AccountSubType": "Inventory"})
        self.qb_rate = f.add("TaxRate", {"Name": "GST 5%", "RateValue": 5})
        self.qb_rate2 = f.add("TaxRate", {"Name": "PST 7%", "RateValue": 7})
        self.qb_taxcode = f.add("TaxCode", {"Name": "GST/PST BC", "Active": True, "Taxable": True,
                                            "SalesTaxRateList": {"TaxRateDetail": [
                                                {"TaxRateRef": {"value": self.qb_rate["Id"]}},
                                                {"TaxRateRef": {"value": self.qb_rate2["Id"]}}]},
                                            "PurchaseTaxRateList": {"TaxRateDetail": [
                                                {"TaxRateRef": {"value": self.qb_rate["Id"]}}]}})
        self.qb_term = f.add("Term", {"Name": "Net 30", "DueDays": 30, "Active": True})
        self.qb_class = f.add("Class", {"Name": "Retail", "FullyQualifiedName": "Retail", "Active": True})
        self.qb_dept = f.add("Department", {"Name": "Vancouver", "FullyQualifiedName": "Vancouver", "Active": True})
        self.qb_customer = f.add("Customer", {
            "DisplayName": "Amy's Bird Sanctuary", "CompanyName": "Amy's Bird Sanctuary",
            "PrimaryEmailAddr": {"Address": "birds@example.com"}, "PrimaryPhone": {"FreeFormNumber": "(650) 555-3311"},
            "BillAddr": {"Line1": "4581 Finch St.", "City": "Bayshore", "CountrySubDivisionCode": "CA",
                         "PostalCode": "94326", "Country": "US"},
            "SalesTermRef": {"value": self.qb_term["Id"]}, "Active": True})
        self.qb_vendor = f.add("Vendor", {"DisplayName": "Books by Bessie", "CompanyName": "Books by Bessie",
                                          "PrimaryEmailAddr": {"Address": "bessie@example.com"}, "AcctNum": "V-77",
                                          "Active": True})
        self.qb_item = f.add("Item", {"Name": "ZQT Garden Rock", "Sku": "ZQT-ROCK-01", "Type": "NonInventory",
                                      "UnitPrice": 25, "PurchaseCost": 10, "Active": True,
                                      "IncomeAccountRef": {"value": self.qb_income["Id"]},
                                      "ExpenseAccountRef": {"value": self.qb_cogs["Id"]},
                                      "SalesTaxCodeRef": {"value": self.qb_taxcode["Id"]}})
        self.qb_service = f.add("Item", {"Name": "Design", "Type": "Service", "UnitPrice": 75, "Active": True,
                                         "IncomeAccountRef": {"value": self.qb_income["Id"]}})

    def qb_invoice(self, **kw):
        vals = {
            "DocNumber": "1037", "TxnDate": "2026-10-01", "DueDate": "2026-10-31",
            "TxnTaxDetail": {"TotalTax": 12.0},  # GST+PST 12% on the taxable 100.00 line
            "CustomerRef": {"value": self.qb_customer["Id"], "name": "Amy's Bird Sanctuary"},
            "CurrencyRef": {"value": self.company.currency_id.name},
            "Line": [
                {"Id": "1", "DetailType": "SalesItemLineDetail", "Amount": 100.0, "Description": "Rocks",
                 "SalesItemLineDetail": {"ItemRef": {"value": self.qb_item["Id"]}, "Qty": 4, "UnitPrice": 25,
                                         "TaxCodeRef": {"value": self.qb_taxcode["Id"]},
                                         "ClassRef": {"value": self.qb_class["Id"]}}},
                {"Id": "2", "DetailType": "SalesItemLineDetail", "Amount": 150.0, "Description": "Design work",
                 "SalesItemLineDetail": {"ItemRef": {"value": self.qb_service["Id"]}, "Qty": 2, "UnitPrice": 75}},
                {"DetailType": "SubTotalLineDetail", "Amount": 250.0, "SubTotalLineDetail": {}},
            ],
        }
        vals.update(kw)
        return vals

    @contextmanager
    def assert_api(self, method, fragment):
        start = len(self.fake.calls)
        yield
        calls = self.fake.calls[start:]
        self.assertTrue(any(c[0] == method and fragment in c[1] for c in calls),
                        "Expected %s %s in calls: %s" % (method, fragment, [(c[0], c[1][-40:]) for c in calls]))
