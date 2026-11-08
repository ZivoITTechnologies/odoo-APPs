import logging
import re

from odoo import api, fields, models

from ..lib import ai as ai_lib, queries as Q

_logger = logging.getLogger(__name__)


def clean_phone(value):
    return re.sub(r"\D", "", value or "")[-10:]


class ZivoShopifyCustomer(models.Model):
    _name = "zivo.shopify.customer"
    _description = "Shopify Customer"
    _order = "id desc"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    partner_id = fields.Many2one("res.partner", required=True, ondelete="cascade", index=True)
    shopify_gid = fields.Char(string="Shopify ID", required=True, index=True, copy=False)
    email = fields.Char()
    orders_count = fields.Integer(readonly=True)
    amount_spent = fields.Float(readonly=True)
    tags = fields.Char(readonly=True)
    match_method = fields.Selection([
        ("created", "New Contact"), ("email", "Email"), ("phone", "Phone"), ("fuzzy", "AI Name + ZIP"),
    ], readonly=True, default="created")
    match_confidence = fields.Float(readonly=True, default=1.0)
    last_sync_at = fields.Datetime(readonly=True)

    _gid_unique = models.Constraint("UNIQUE(instance_id, shopify_gid)", "Customer already linked.")

    def action_export(self):
        for customer in self:
            customer.instance_id._enqueue(
                "export_customer", f"Export {customer.name}", {"partner": customer.partner_id.id},
                dedupe_key=f"export_customer:{customer.partner_id.id}", source="odoo",
            )
        return True


class ResPartner(models.Model):
    _inherit = "res.partner"

    zivo_shopify_customer_ids = fields.One2many("zivo.shopify.customer", "partner_id", string="Shopify Customers")

    # ------------------------------------------------------------------
    # Resolving contacts
    # ------------------------------------------------------------------
    @api.model
    def _zivo_country_state(self, address):
        country = self.env["res.country"]
        state = self.env["res.country.state"]
        code = (address or {}).get("countryCodeV2")
        if code:
            country = country.search([("code", "=", code)], limit=1)
        province = (address or {}).get("provinceCode")
        if country and (province or (address or {}).get("province")):
            state = state.search([("country_id", "=", country.id), ("code", "=", province)], limit=1) if province else state
            if not state and address.get("province"):
                state = self.env["res.country.state"].search(
                    [("country_id", "=", country.id), ("name", "=ilike", address["province"])], limit=1)
        return country, state

    @api.model
    def _zivo_address_vals(self, address):
        country, state = self._zivo_country_state(address)
        return {
            "street": address.get("address1") or False,
            "street2": address.get("address2") or False,
            "city": address.get("city") or False,
            "zip": address.get("zip") or False,
            "country_id": country.id or False,
            "state_id": state.id or False,
        }

    @api.model
    def _zivo_person_name(self, node, address=None):
        first = (node or {}).get("firstName") or (address or {}).get("firstName") or ""
        last = (node or {}).get("lastName") or (address or {}).get("lastName") or ""
        return f"{first} {last}".strip() or (node or {}).get("displayName") or (address or {}).get("name") or ""

    @api.model
    def _zivo_match(self, instance, email, phone, name, zip_code):
        """Return ``(partner, method, confidence)`` for an unknown Shopify customer."""
        # Delivery and invoice addresses belong to someone else's contact card.
        company = [("company_id", "in", [instance.company_id.id, False]), ("type", "not in", ("delivery", "invoice"))]
        if email:
            partner = self.search([("email", "=ilike", email)] + company, limit=1, order="id")
            if partner:
                return partner, "email", 1.0
        digits = clean_phone(phone)
        if len(digits) >= 8:
            # Stored numbers may contain spaces, so only the last digits can be searched.
            for partner in self.search([("phone", "like", digits[-4:])] + company, limit=200):
                # Families and offices share phones: a different email means a different person.
                if clean_phone(partner.phone) == digits and not (email and partner.email):
                    return partner, "phone", 0.95
        if name and zip_code:
            best, best_score = self.browse(), 0.0
            for partner in self.search([("zip", "=", zip_code), ("is_company", "=", False)] + company, limit=50):
                score = ai_lib.similarity(name, partner.name or "")
                if score > best_score:
                    best, best_score = partner, score
            if best and best_score >= 0.92:
                return best, "fuzzy", best_score
        return self.browse(), "created", 1.0

    @api.model
    def _zivo_upsert_customer(self, instance, node, address=None):
        """Find or create the Odoo contact of a Shopify customer."""
        Customer = self.env["zivo.shopify.customer"]
        mapping = Customer.search([("instance_id", "=", instance.id), ("shopify_gid", "=", node["id"])], limit=1)
        address = address or node.get("defaultAddress") or {}
        email = (node.get("defaultEmailAddress") or {}).get("emailAddress")
        phone = (node.get("defaultPhoneNumber") or {}).get("phoneNumber") or address.get("phone")
        name = self._zivo_person_name(node, address) or email or node["id"]
        partner = mapping.partner_id
        method, confidence = mapping.match_method or "created", mapping.match_confidence or 1.0
        if not partner:
            partner, method, confidence = self._zivo_match(instance, email, phone, name, address.get("zip"))
        if not partner:
            partner = self.create({
                "name": name, "email": email, "phone": phone, "customer_rank": 1,
                "company_id": False, "lang": self._zivo_lang(node.get("locale")),
                **self._zivo_address_vals(address),
            })
            self._zivo_attach_company(instance, partner, address)
        vals = {
            "name": name,
            "email": email,
            "orders_count": int(node.get("numberOfOrders") or 0),
            "amount_spent": float((node.get("amountSpent") or {}).get("amount") or 0),
            "tags": ", ".join(node.get("tags") or []),
            "last_sync_at": fields.Datetime.now(),
        }
        if mapping:
            mapping.write(vals)
        else:
            mapping = Customer.create({
                **vals, "instance_id": instance.id, "shopify_gid": node["id"], "partner_id": partner.id,
                "match_method": method, "match_confidence": confidence,
            })
        self.env["zivo.shopify.metafield.mapping"]._apply_import(
            instance, "customer", partner, (node.get("metafields") or {}).get("nodes"))
        return partner

    @api.model
    def _zivo_lang(self, locale):
        if not locale:
            return False
        code = locale.replace("-", "_")
        languages = self.env["res.lang"].search([("code", "=ilike", f"{code}%")], limit=1)
        return languages.code or False

    @api.model
    def _zivo_attach_company(self, instance, partner, address, company_name=None):
        company_name = company_name or (address or {}).get("company")
        if instance.company_mode != "b2b" or not company_name or partner.parent_id:
            return
        company = self.search([("name", "=ilike", company_name), ("is_company", "=", True)], limit=1) or self.create({
            "name": company_name, "is_company": True, "customer_rank": 1, **self._zivo_address_vals(address or {}),
        })
        partner.parent_id = company

    @api.model
    def _zivo_child_address(self, parent, address, kind):
        """Reuse or create the delivery/invoice contact matching an address."""
        if not address:
            return parent
        vals = self._zivo_address_vals(address)
        name = address.get("name") or self._zivo_person_name(None, address) or parent.name
        if all((parent[key].id if key.endswith("_id") and parent[key] else parent[key]) == vals[key] for key in vals):
            return parent
        domain = [("parent_id", "=", parent.id), ("type", "=", kind), ("name", "=", name)]
        for key, value in vals.items():
            domain.append((key, "=", value))
        return self.search(domain, limit=1) or self.create({
            **vals, "parent_id": parent.id, "type": kind, "name": name, "phone": address.get("phone"),
        })

    @api.model
    def _zivo_order_partners(self, instance, node):
        """Return ``(customer, invoice_contact, delivery_contact)`` for an order."""
        shipping = node.get("shippingAddress") or {}
        billing = node.get("billingAddress") or shipping
        customer = node.get("customer")
        if customer:
            partner = self._zivo_upsert_customer(instance, customer, billing or shipping)
        else:
            email = node.get("email")
            name = self._zivo_person_name(None, billing or shipping) or email or node["name"]
            partner, _method, _score = self._zivo_match(instance, email, node.get("phone"), name, (billing or {}).get("zip"))
            if not partner:
                partner = self.create({
                    "name": name, "email": email, "phone": node.get("phone") or (billing or {}).get("phone"),
                    "customer_rank": 1, **self._zivo_address_vals(billing or {}),
                })
        entity = node.get("purchasingEntity") or {}
        company_name = (entity.get("company") or {}).get("name")
        self._zivo_attach_company(instance, partner, billing or shipping, company_name)
        return partner, self._zivo_child_address(partner, billing, "invoice"), self._zivo_child_address(partner, shipping, "delivery")

    def action_zivo_export_customer(self):
        instances = self.env["zivo.shopify.instance"].search([("state", "=", "connected")])
        for partner in self:
            for instance in instances.filtered(lambda i: not partner.company_id or i.company_id == partner.company_id):
                instance._enqueue("export_customer", f"Export {partner.name}", {"partner": partner.id},
                                  dedupe_key=f"export_customer:{partner.id}", source="odoo")
        return True


class ZivoShopifyInstanceCustomers(models.Model):
    _inherit = "zivo.shopify.instance"

    def action_import_customers(self):
        self.ensure_one()
        self._enqueue("import_customers", "Import customers", {}, dedupe_key="import_customers")
        return self._notify(self.env._("Customer import queued."), "success")

    def action_import_segments(self):
        self.ensure_one()
        self._enqueue("import_segments", "Import customer segments", {}, dedupe_key="import_segments")
        return self._notify(self.env._("Segment import queued."), "success")

    def _job_import_customers(self, job):
        payload = job.payload or {}
        data = self._client().execute(Q.CUSTOMERS, {"query": payload.get("query"), "cursor": payload.get("cursor")}, expected_cost=700)["customers"]
        Partner = self.env["res.partner"]
        for node in data["nodes"]:
            Partner._zivo_upsert_customer(self, node)
        info = data["pageInfo"]
        if info["hasNextPage"]:
            self._enqueue("import_customers", "Import customers (next page)", {**payload, "cursor": info["endCursor"]}, source=job.source)
        else:
            self.last_customer_sync = fields.Datetime.now()
        return f"{len(data['nodes'])} customers"

    def _job_import_customer(self, job):
        node = self._client().execute(Q.CUSTOMER, {"id": job.payload["gid"]}).get("customer")
        if not node:
            return "Customer no longer exists"
        return self.env["res.partner"]._zivo_upsert_customer(self, node).display_name

    def _job_export_customer(self, job):
        partner = self.env["res.partner"].browse(job.payload["partner"]).exists()
        if not partner:
            return "Contact removed"
        parts = (partner.name or "").split(" ", 1)
        input_vals = {
            "firstName": parts[0],
            "lastName": parts[1] if len(parts) > 1 else parts[0],
            "note": partner.comment and str(partner.comment)[:5000] or None,
        }
        if partner.email:
            input_vals["email"] = partner.email
        if partner.phone:
            input_vals["phone"] = partner.phone
        if partner.street and partner.country_id:
            input_vals["addresses"] = [{
                "address1": partner.street, "address2": partner.street2 or "", "city": partner.city or "",
                "zip": partner.zip or "", "countryCode": partner.country_id.code,
                "provinceCode": partner.state_id.code or None,
                "firstName": input_vals["firstName"], "lastName": input_vals["lastName"],
            }]
        mapping = partner.zivo_shopify_customer_ids.filtered(lambda c: c.instance_id == self)[:1]
        if mapping:
            identifier = {"id": mapping.shopify_gid}
        elif partner.email:
            identifier = {"email": partner.email}
        else:
            identifier = None
        payload = self._client().mutate(Q.CUSTOMER_SET, {"input": input_vals, "identifier": identifier}, "customerSet")
        gid = payload["customer"]["id"]
        if not mapping:
            self.env["zivo.shopify.customer"].create({
                "instance_id": self.id, "shopify_gid": gid, "partner_id": partner.id,
                "name": partner.name, "email": partner.email, "last_sync_at": fields.Datetime.now(),
            })
        return gid

    def _job_import_segments(self, job):
        client = self._client()
        Tag = self.env["res.partner.category"]
        Customer = self.env["zivo.shopify.customer"]
        count = 0
        for segment in client.iter_connection(Q.SEGMENTS, {}, ["segments"], expected_cost=200):
            name = f"Shopify: {segment['name']}"
            tag = Tag.search([("name", "=", name)], limit=1) or Tag.create({"name": name})
            variables = {"segment": segment["id"]}
            while True:
                data = client.execute(Q.SEGMENT_MEMBERS, variables)["customerSegmentMembers"]
                gids = []
                for edge in data["edges"]:
                    gids.append(edge["node"]["id"].replace("CustomerSegmentMember", "Customer"))
                customers = Customer.search([("instance_id", "=", self.id), ("shopify_gid", "in", gids)])
                customers.partner_id.write({"category_id": [fields.Command.link(tag.id)]})
                count += len(customers)
                if not data["pageInfo"]["hasNextPage"]:
                    break
                variables["cursor"] = data["pageInfo"]["endCursor"]
        return f"{count} contacts tagged"
