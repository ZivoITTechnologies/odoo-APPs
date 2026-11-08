"""An in-memory Shopify store used by the test-suite and the demo server.

When the ``ZIVO_SHOPIFY_SCHEMA`` environment variable points to an introspection
result of the Admin API, and ``graphql-core`` is importable, every request is also
validated and executed against that real schema: wrong fields, wrong input shapes
and missing non-null values then fail exactly as they would on Shopify.
"""
import copy
import json
import os
import re

SCHEMA = None
_path = os.environ.get("ZIVO_SHOPIFY_SCHEMA")
if _path and os.path.exists(_path):
    try:
        from graphql import build_client_schema, graphql_sync
        with open(_path) as handle:
            SCHEMA = build_client_schema(json.load(handle)["data"])
        # Shopify's own schema breaks one interface rule that graphql-core enforces.
        SCHEMA._validation_errors = []
    except ImportError:
        SCHEMA = None



def estimate_cost(schema, document):
    """Shopify's requested cost: object 1, scalar 0, connection 2 + n x node, mutation +10."""
    from graphql import (
        FieldNode, FragmentSpreadNode, get_named_type, get_nullable_type, is_abstract_type, is_list_type, parse,
    )
    parsed = parse(document)
    fragments = {d.name.value: d for d in parsed.definitions if d.kind == "fragment_definition"}

    def field_cost(node, parent):
        if not node.selection_set or not hasattr(parent, "fields") or node.name.value not in parent.fields:
            return 0
        definition = parent.fields[node.name.value]
        named = get_named_type(definition.type)
        size = next((int(a.value.value) for a in node.arguments or () if a.name.value in ("first", "last") and hasattr(a.value, "value")), 1)
        if named.name.endswith("Connection"):
            unit = sum(field_cost(x, named) for x in node.selection_set.selections if isinstance(x, FieldNode) and x.name.value in ("nodes", "edges"))
            return 2 + unit * size
        inner = selection_cost(node.selection_set, named)
        return (1 + inner) * size if is_list_type(get_nullable_type(definition.type)) else 1 + inner

    def selection_cost(selection_set, parent):
        best = 0
        for concrete in (schema.get_possible_types(parent) if is_abstract_type(parent) else [parent]):
            total = 0
            for selection in selection_set.selections:
                if isinstance(selection, FieldNode):
                    total += 0 if selection.name.value == "__typename" else field_cost(selection, concrete)
                    continue
                fragment = fragments[selection.name.value] if isinstance(selection, FragmentSpreadNode) else selection
                condition = schema.type_map[fragment.type_condition.name.value] if fragment.type_condition else concrete
                if condition is concrete or (is_abstract_type(condition) and schema.is_sub_type(condition, concrete)):
                    total += selection_cost(fragment.selection_set, concrete)
            best = max(best, total)
        return best

    operation = next(d for d in parsed.definitions if d.kind == "operation_definition")
    root = schema.mutation_type if operation.operation.value == "mutation" else schema.query_type
    return selection_cost(operation.selection_set, root) + (10 if operation.operation.value == "mutation" else 0)


OP_RE = re.compile(r"(?:query|mutation)\s+(\w+)")


def money(amount, code="USD", presentment=None, presentment_code=None):
    return {
        "shopMoney": {"amount": f"{amount:.2f}", "currencyCode": code},
        "presentmentMoney": {"amount": f"{(presentment if presentment is not None else amount):.2f}", "currencyCode": presentment_code or code},
    }


def nodes(items):
    return {"nodes": items, "pageInfo": {"hasNextPage": False, "endCursor": None}}


def address(first="Asha", last="Rao", city="Hyderabad", zip_code="500049", country="IN", province="TS", company=None, line="12 Lake Road"):
    return {
        "firstName": first, "lastName": last, "name": f"{first} {last}", "company": company,
        "address1": line, "address2": None, "city": city, "province": "Telangana", "provinceCode": province,
        "zip": zip_code, "country": "India", "countryCodeV2": country, "phone": "+919876543210",
    }


def make_customer(gid="1", email="asha@example.com", orders=3, first="Asha", last="Rao", phone="+919876543210", addr=None):
    return {
        "id": f"gid://shopify/Customer/{gid}", "firstName": first, "lastName": last, "displayName": f"{first} {last}",
        "locale": "en", "note": None, "tags": ["vip"], "taxExempt": False, "numberOfOrders": str(orders),
        "updatedAt": "2026-11-01T10:00:00Z",
        "defaultEmailAddress": {"emailAddress": email} if email else None,
        "defaultPhoneNumber": {"phoneNumber": phone} if phone else None,
        "defaultAddress": addr if addr is not None else address(first, last),
        "amountSpent": {"amount": "450.00", "currencyCode": "USD"},
        "metafields": nodes([]),
    }


def make_variant(gid, sku, price="25.00", options=None, title="Default Title", barcode=None, position=1):
    return {
        "id": f"gid://shopify/ProductVariant/{gid}", "title": title, "sku": sku, "price": price, "compareAtPrice": None,
        "position": position, "taxable": True, "inventoryPolicy": "DENY",
        "barcodes": nodes([{"value": barcode}] if barcode else []),
        "selectedOptions": options or [{"name": "Title", "value": "Default Title"}],
        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{gid}", "tracked": True, "unitCost": {"amount": "10.00"}},
    }


def full_options(options, product_gid):
    """Add the IDs Shopify returns on options and their values."""
    result = []
    for index, option in enumerate(options):
        option_id = f"gid://shopify/ProductOption/{product_gid}{index}"
        result.append({
            **option, "id": option_id,
            "optionValues": [{"id": f"gid://shopify/ProductOptionValue/{product_gid}{index}{pos}", "name": value}
                             for pos, value in enumerate(option["values"])],
        })
    return result


def make_collection(gid, title, smart=False):
    return {
        "id": f"gid://shopify/Collection/{gid}", "title": title, "handle": title.lower().replace(" ", "-"),
        "sources": [{"__typename": "CollectionConditionsSource", "id": f"gid://shopify/CollectionConditionsSource/{gid}", "title": "Rules"}] if smart else [],
    }


def make_product(gid, title, variants, options=None, status="ACTIVE", metafields=None, collections=None):
    return {
        "id": f"gid://shopify/Product/{gid}", "title": title, "handle": title.lower().replace(" ", "-"), "status": status,
        "vendor": "Acme", "productType": "Apparel", "tags": ["summer"], "descriptionHtml": f"<p>{title} description</p>",
        "updatedAt": "2026-11-01T10:00:00Z", "seo": {"title": title, "description": "Great"},
        "options": full_options(options or [{"name": "Title", "position": 1, "values": ["Default Title"]}], gid),
        "media": nodes([]), "metafields": nodes(metafields or []), "collections": nodes(collections or []),
        "variants": nodes(variants),
    }


def make_line(gid, variant_gid, qty=2, price=100.0, sku="SKU-1", discount=0.0, tax_rate=0.18, tax=36.0, current=None, name="Blue Tee"):
    return {
        "id": f"gid://shopify/LineItem/{gid}", "name": name, "title": name, "sku": sku, "quantity": qty,
        "currentQuantity": qty if current is None else current, "requiresShipping": True, "taxable": True,
        "originalUnitPriceSet": money(price), "totalDiscountSet": money(discount),
        "taxLines": [{"title": "GST", "rate": tax_rate, "priceSet": money(tax)}] if tax_rate else [],
        "variant": {"id": f"gid://shopify/ProductVariant/{variant_gid}"} if variant_gid else None,
    }


def make_transaction(gid="1", amount=236.0, kind="SALE", status="SUCCESS", gateway="shopify_payments", fee=7.0):
    return {
        "id": f"gid://shopify/OrderTransaction/{gid}", "kind": kind, "status": status, "gateway": gateway,
        "processedAt": "2026-11-05T10:01:00Z", "test": False, "amountSet": money(amount), "parentTransaction": None,
        "fees": [{"amount": {"amount": f"{fee:.2f}", "currencyCode": "USD"}}] if fee else [],
    }


def make_order(gid="1001", lines=None, total=236.0, transactions=None, name=None, **overrides):
    customer = overrides.pop("customer", make_customer())
    order = {
        "id": f"gid://shopify/Order/{gid}", "name": name or f"#{gid}", "createdAt": "2026-11-05T10:00:00Z",
        "updatedAt": "2026-11-05T10:00:00Z", "processedAt": "2026-11-05T10:00:00Z", "cancelledAt": None, "cancelReason": None,
        "closedAt": None, "test": False, "edited": False, "note": None, "tags": ["web"], "email": "asha@example.com",
        "phone": None, "sourceName": "web", "taxesIncluded": False, "currencyCode": "USD", "presentmentCurrencyCode": "USD",
        "paymentGatewayNames": ["shopify_payments"], "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED",
        "discountCodes": [], "poNumber": None, "billingAddressMatchesShippingAddress": True, "retailLocation": None,
        "totalPriceSet": money(total), "currentTotalPriceSet": money(total), "totalTaxSet": money(36.0),
        "totalTipReceivedSet": money(0.0), "totalRefundedSet": money(0.0),
        "customer": customer, "purchasingEntity": {"__typename": "Customer"},
        "billingAddress": address(), "shippingAddress": address(),
        "risk": {"recommendation": "ACCEPT", "assessments": [{"riskLevel": "LOW", "facts": []}]},
        "metafields": nodes([]),
        "lineItems": nodes(lines if lines is not None else [make_line("1", "11")]),
        "shippingLines": nodes([]),
        "transactions": transactions if transactions is not None else [make_transaction(amount=total)],
        "fulfillments": [], "refunds": [], "returns": nodes([]), "returnStatus": "NO_RETURN",
    }
    order.update(overrides)
    return order


class MockResponse:
    def __init__(self, status_code, payload=None, headers=None, text=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload or {})
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class ShopifyMock:
    """Stateful fake of the Admin API. ``calls`` keeps every operation for assertions."""

    def __init__(self):
        self.products = {}
        self.orders = {}
        self.customers = {}
        self.locations = [{
            "id": "gid://shopify/Location/1", "name": "Main Hub", "isActive": True, "fulfillsOnlineOrders": True,
            "isFulfillmentService": False,
            "address": {"address1": "1 Hub", "city": "Hyderabad", "zip": "500049", "province": "Telangana", "country": "India", "countryCode": "IN"},
        }]
        self.levels = {}
        self.webhooks = []
        self.fulfillment_orders = {}
        self.payouts = []
        self.balance = []
        self.checkouts = []
        self.markets = []
        self.segments = []
        self.segment_members = {}
        self.calls = []
        self.queue = []  # scripted (status, payload, headers) responses for failure tests
        self.not_stocked_once = False
        self.seq = 5000
        self.llm_answer = '{"root_cause": "x", "fix": "y"}'
        self.llm_requests = []
        self.token_requests = 0
        self.bad_token = None
        self.collections = []
        self.locales = [{"locale": "en", "name": "English", "primary": True, "published": True}]
        self.translations = {}
        self.outdated = set()

    def next_id(self):
        self.seq += 1
        return self.seq

    def ops(self, name):
        return [call for call in self.calls if call[0] == name]

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def post(self, url, json=None, headers=None, timeout=None, data=None, **_kw):
        if "/admin/oauth/access_token" in url:
            self.token_requests += 1
            return MockResponse(200, {"access_token": f"tok-{self.token_requests}", "expires_in": 86399, "scope": "read_orders"})
        if "chat/completions" in url:
            self.llm_requests.append(("openai", url, headers, json))
            return MockResponse(200, {"choices": [{"message": {"content": self.llm_answer}}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}})
        if "/v1/messages" in url:
            self.llm_requests.append(("anthropic", url, headers, json))
            return MockResponse(200, {"content": [{"type": "text", "text": self.llm_answer}], "usage": {"input_tokens": 11, "output_tokens": 7}})
        if ":generateContent" in url:
            self.llm_requests.append(("gemini", url, headers, json))
            return MockResponse(200, {"candidates": [{"content": {"parts": [{"text": self.llm_answer}]}}], "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7}})
        if self.queue:
            status, payload, response_headers = self.queue.pop(0)
            return MockResponse(status, payload, response_headers)
        if self.bad_token and (headers or {}).get("X-Shopify-Access-Token") == self.bad_token:
            return MockResponse(401, {"errors": "Invalid"})
        return MockResponse(200, self.execute(json["query"], json.get("variables") or {}))

    def get(self, url, timeout=None, **_kw):
        return MockResponse(200, {}, content=__import__("base64").b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))

    def execute(self, query, variables):
        name = OP_RE.search(query).group(1)
        self.calls.append((name, copy.deepcopy(variables)))
        handler = getattr(self, f"op_{name}", None)
        if handler is None:
            raise AssertionError(f"Mock does not implement {name}")
        root = handler(variables)
        result = {"data": root}
        if SCHEMA is not None:
            executed = graphql_sync(SCHEMA, source=query, root_value=root, variable_values=variables)
            if executed.errors:
                raise AssertionError(f"{name} violates the Shopify schema: {[e.message for e in executed.errors]}")
            result = {"data": executed.data}
        result["extensions"] = {"cost": {
            "requestedQueryCost": 10, "actualQueryCost": 5,
            "throttleStatus": {"maximumAvailable": 2000.0, "currentlyAvailable": 1800, "restoreRate": 100.0},
        }}
        return result

    # ------------------------------------------------------------------
    # Store & locations
    # ------------------------------------------------------------------
    def op_ZivoShop(self, _v):
        return {"shop": {
            "id": "gid://shopify/Shop/1", "name": "Acme Store", "email": "owner@acme.test", "myshopifyDomain": "acme.myshopify.com",
            "currencyCode": "USD", "ianaTimezone": "Asia/Kolkata", "taxesIncluded": False, "weightUnit": "KILOGRAMS",
            "enabledPresentmentCurrencies": ["USD", "EUR"],
            "primaryDomain": {"url": "https://acme.test", "host": "acme.test"},
            "plan": {"publicDisplayName": "Advanced", "shopifyPlus": False},
        }}

    def op_ZivoLocations(self, _v):
        return {"locations": nodes(self.locations)}

    def op_ZivoPublications(self, _v):
        return {"publications": nodes([{"id": "gid://shopify/Publication/1", "catalog": {"__typename": "AppCatalog", "title": "Online Store"}}])}

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------
    def op_ZivoProducts(self, _v):
        return {"products": nodes(list(self.products.values()))}

    def op_ZivoProduct(self, v):
        return {"product": next((p for p in self.products.values() if p["id"] == v["id"]), None)}

    def op_ZivoVariantProduct(self, v):
        for product in self.products.values():
            for variant in product["variants"]["nodes"]:
                if variant["id"] == v["id"]:
                    return {"productVariant": {"id": v["id"], "product": {"id": product["id"]}}}
        return {"productVariant": None}

    def op_ZivoProductSet(self, v):
        data = v["input"]
        gid = (v.get("identifier") or {}).get("id") or f"gid://shopify/Product/{self.next_id()}"
        variants = []
        for index, item in enumerate(data["variants"]):
            variant_id = self.next_id()
            selected = [{"name": o["optionName"], "value": o["name"]} for o in item["optionValues"]]
            variant = make_variant(variant_id, item.get("sku"), item.get("price", "0"), selected,
                                   " / ".join(o["value"] for o in selected), item.get("barcode"), index + 1)
            if item.get("id"):
                variant["id"] = item["id"]
            variants.append(variant)
        options = [{"name": o["name"], "position": o.get("position", 1), "values": [x["name"] for x in o["values"]]} for o in data["productOptions"]]
        product = make_product(gid.rsplit("/", 1)[-1], data["title"], variants, options, data.get("status", "DRAFT"))
        self.products[product["id"]] = product
        return {"productSet": {"product": product, "userErrors": []}}

    def op_ZivoProductStatus(self, v):
        product = self.products[v["product"]["id"]]
        product["status"] = v["product"]["status"]
        return {"productUpdate": {"product": {"id": product["id"], "status": product["status"]}, "userErrors": []}}

    def op_ZivoVariantPrices(self, v):
        return {"productVariantsBulkUpdate": {"productVariants": [
            {"id": x["id"], "price": x["price"], "compareAtPrice": x.get("compareAtPrice")} for x in v["variants"]
        ], "userErrors": []}}

    def op_ZivoPublish(self, _v):
        return {"publishablePublish": {"userErrors": []}}

    def op_ZivoUnpublish(self, _v):
        return {"publishableUnpublish": {"userErrors": []}}

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def op_ZivoInventorySet(self, v):
        if self.not_stocked_once:
            self.not_stocked_once = False
            return {"inventorySetQuantities": {"inventoryAdjustmentGroup": None, "userErrors": [
                {"field": ["input"], "message": "The specified inventory item is not stocked at the location.", "code": "INVALID_LOCATION"},
            ]}}
        for line in v["input"]["quantities"]:
            self.levels[(line["inventoryItemId"], line["locationId"])] = line["quantity"]
        return {"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "gid://shopify/InventoryAdjustmentGroup/1"}, "userErrors": []}}

    def op_ZivoInventoryActivate(self, _v):
        return {"inventoryActivate": {"inventoryLevel": {"id": "gid://shopify/InventoryLevel/1"}, "userErrors": []}}

    def op_ZivoInventoryLevels(self, v):
        items = [
            {"item": {"id": item}, "quantities": [{"name": "available", "quantity": qty}, {"name": "on_hand", "quantity": qty}]}
            for (item, location), qty in self.levels.items() if location == v["location"]
        ]
        return {"location": {"inventoryLevels": nodes(items)}}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def op_ZivoOrders(self, _v):
        return {"orders": nodes(list(self.orders.values()))}

    def op_ZivoOrderOne(self, v):
        order = self.orders.get(v["id"])
        if order and order["returns"]["nodes"]:
            order = {**order, "returnStatus": "RETURN_REQUESTED"}
        return {"order": order}

    def op_ZivoOrderLines(self, v):
        return {"order": {"lineItems": self.orders[v["id"]]["lineItems"]}}

    def op_ZivoOrderReturns(self, v):
        return {"order": {"returns": self.orders[v["id"]]["returns"]}}

    def _find_child(self, key, gid):
        for order in self.orders.values():
            for item in order[key]:
                if item["id"] == gid:
                    return item
        return None

    def op_ZivoFulfillmentNode(self, v):
        return {"node": {**self._find_child("fulfillments", v["id"]), "__typename": "Fulfillment"}}

    def op_ZivoRefundNode(self, v):
        return {"node": {**self._find_child("refunds", v["id"]), "__typename": "Refund"}}

    def op_ZivoFulfillmentOrderLines(self, v):
        return {"fulfillmentOrder": {"lineItems": nodes([])}}

    # ------------------------------------------------------------------
    # Collections & translations
    # ------------------------------------------------------------------
    def op_ZivoCollections(self, _v):
        return {"collections": nodes(self.collections)}

    def op_ZivoCollectionCreate(self, v):
        collection = make_collection(self.next_id(), v["collection"]["title"])
        self.collections.append(collection)
        return {"collectionCreate": {"collection": collection, "userErrors": []}}

    def op_ZivoShopLocales(self, _v):
        return {"shopLocales": self.locales}

    def _source_content(self, gid):
        for product in self.products.values():
            if product["id"] == gid:
                return {"title": product["title"], "body_html": product["descriptionHtml"], "meta_title": product["seo"]["title"], "meta_description": product["seo"]["description"]}
            for option in product["options"]:
                if option["id"] == gid:
                    return {"name": option["name"]}
                for value in option["optionValues"]:
                    if value["id"] == gid:
                        return {"name": value["name"]}
        for collection in self.collections:
            if collection["id"] == gid:
                return {"title": collection["title"]}
        return {}

    @staticmethod
    def digest(value):
        import hashlib
        return hashlib.sha256((value or "").encode()).hexdigest()

    def op_ZivoTranslatable(self, v):
        items = []
        for gid in v["ids"]:
            content = self._source_content(gid)
            items.append({
                "resourceId": gid,
                "translatableContent": [{"key": key, "value": value, "digest": self.digest(value), "locale": "en"} for key, value in content.items()],
                "translations": [
                    {"key": key, "value": value, "locale": v["locale"], "outdated": (gid, key) in self.outdated}
                    for key, value in self.translations.get((gid, v["locale"]), {}).items()
                ],
            })
        return {"translatableResourcesByIds": nodes(items)}

    def op_ZivoTranslationsRegister(self, v):
        content = self._source_content(v["resourceId"])
        for item in v["translations"]:
            if item["translatableContentDigest"] != self.digest(content.get(item["key"])):
                return {"translationsRegister": {"translations": None, "userErrors": [{"field": ["digest"], "message": "Digest mismatch", "code": "INVALID_TRANSLATABLE_CONTENT"}]}}
            self.translations.setdefault((v["resourceId"], item["locale"]), {})[item["key"]] = item["value"]
        return {"translationsRegister": {"translations": [{"key": i["key"], "locale": i["locale"]} for i in v["translations"]], "userErrors": []}}

    def op_ZivoOrderCancel(self, v):
        order = self.orders[v["orderId"]]
        order["cancelledAt"] = "2026-11-06T10:00:00Z"
        order["cancelReason"] = v["reason"]
        return {"orderCancel": {"job": {"id": "gid://shopify/Job/1", "done": False}, "orderCancelUserErrors": []}}

    def op_ZivoOrderCreate(self, _v):
        return {"orderCreate": {"order": {"id": f"gid://shopify/Order/{self.next_id()}", "name": "#9001"}, "userErrors": []}}

    def op_ZivoOrderEditBegin(self, v):
        order = self.orders[v["id"]]
        lines = [{"id": line["id"].replace("LineItem", "CalculatedLineItem"), "quantity": line["currentQuantity"], "variant": line["variant"]} for line in order["lineItems"]["nodes"]]
        return {"orderEditBegin": {"calculatedOrder": {"id": "gid://shopify/CalculatedOrder/1", "lineItems": nodes(lines)}, "userErrors": []}}

    def op_ZivoOrderEditSetQty(self, _v):
        return {"orderEditSetQuantity": {"calculatedOrder": {"id": "gid://shopify/CalculatedOrder/1"}, "userErrors": []}}

    def op_ZivoOrderEditAddVariant(self, _v):
        return {"orderEditAddVariant": {"calculatedOrder": {"id": "gid://shopify/CalculatedOrder/1"}, "userErrors": []}}

    def op_ZivoOrderEditCommit(self, _v):
        return {"orderEditCommit": {"order": {"id": "gid://shopify/Order/1"}, "userErrors": []}}

    def op_ZivoFulfillmentOrders(self, v):
        order = self.orders[v["id"]]
        default = [{
            "id": f"gid://shopify/FulfillmentOrder/{order['id'].rsplit('/', 1)[-1]}", "status": "OPEN",
            "supportedActions": [{"action": "CREATE_FULFILLMENT"}],
            "assignedLocation": {"location": {"id": "gid://shopify/Location/1"}},
            "lineItems": nodes([
                {"id": line["id"].replace("LineItem", "FulfillmentOrderLineItem"), "remainingQuantity": line["currentQuantity"],
                 "totalQuantity": line["quantity"], "lineItem": {"id": line["id"]}}
                for line in order["lineItems"]["nodes"]
            ]),
        }]
        return {"order": {"id": order["id"], "fulfillmentOrders": nodes(self.fulfillment_orders.get(v["id"], default))}}

    def op_ZivoFulfillmentCreate(self, _v):
        return {"fulfillmentCreate": {"fulfillment": {"id": f"gid://shopify/Fulfillment/{self.next_id()}", "status": "SUCCESS"}, "userErrors": []}}

    def op_ZivoSuggestedRefund(self, v):
        return {"order": {"suggestedRefund": {"suggestedTransactions": [{
            "gateway": "shopify_payments", "kind": "SUGGESTED_REFUND", "amountSet": money(118.0),
            "parentTransaction": {"id": "gid://shopify/OrderTransaction/1"},
        }]}}}

    def op_ZivoRefundCreate(self, _v):
        return {"refundCreate": {"refund": {"id": f"gid://shopify/Refund/{self.next_id()}"}, "userErrors": []}}

    def op_ZivoReturnCreate(self, _v):
        return {"returnCreate": {"return": {"id": f"gid://shopify/Return/{self.next_id()}", "name": "#R1", "status": "OPEN"}, "userErrors": []}}

    # ------------------------------------------------------------------
    # Customers & commerce
    # ------------------------------------------------------------------
    def op_ZivoCustomers(self, _v):
        return {"customers": nodes(list(self.customers.values()))}

    def op_ZivoCustomerOne(self, v):
        return {"customer": self.customers.get(v["id"])}

    def op_ZivoCustomerSet(self, _v):
        return {"customerSet": {"customer": {"id": f"gid://shopify/Customer/{self.next_id()}"}, "userErrors": []}}

    def op_ZivoSegments(self, _v):
        return {"segments": nodes(self.segments)}

    def op_ZivoSegmentMembers(self, v):
        members = self.segment_members.get(v["segment"], [])
        return {"customerSegmentMembers": {
            "edges": [{"node": {"id": gid, "defaultEmailAddress": None}} for gid in members],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}

    def op_ZivoCheckouts(self, _v):
        return {"abandonedCheckouts": nodes(self.checkouts)}

    def op_ZivoPayouts(self, _v):
        return {"shopifyPaymentsAccount": {"payouts": nodes(self.payouts)}}

    def op_ZivoBalanceTransactions(self, _v):
        return {"shopifyPaymentsAccount": {"balanceTransactions": nodes(self.balance)}}

    def op_ZivoMarkets(self, _v):
        return {"markets": nodes(self.markets)}

    def op_ZivoPriceListAdd(self, v):
        return {"priceListFixedPricesAdd": {"prices": [{"price": {"amount": p["price"]["amount"], "currencyCode": p["price"]["currencyCode"]}} for p in v["prices"]], "userErrors": []}}

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------
    def op_ZivoWebhooks(self, _v):
        return {"webhookSubscriptions": nodes(self.webhooks)}

    def op_ZivoWebhookCreate(self, v):
        hook = {"id": f"gid://shopify/WebhookSubscription/{self.next_id()}", "topic": v["topic"], "uri": v["sub"]["uri"]}
        self.webhooks.append(hook)
        return {"webhookSubscriptionCreate": {"webhookSubscription": hook, "userErrors": []}}

    def op_ZivoWebhookDelete(self, v):
        self.webhooks = [hook for hook in self.webhooks if hook["id"] != v["id"]]
        return {"webhookSubscriptionDelete": {"deletedWebhookSubscriptionId": v["id"], "userErrors": []}}
