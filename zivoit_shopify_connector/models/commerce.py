import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models

from ..lib import ai as ai_lib, queries as Q

_logger = logging.getLogger(__name__)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    zivo_instance_id = fields.Many2one("zivo.shopify.instance", string="Shopify Store", index=True, copy=False, readonly=True)
    zivo_checkout_gid = fields.Char(index=True, copy=False, readonly=True)
    zivo_recovery_url = fields.Char(string="Recovery Link", copy=False, readonly=True)
    zivo_cart_summary = fields.Text(string="Abandoned Cart", copy=False, readonly=True)
    zivo_recovery_email = fields.Html(string="AI Recovery Email", copy=False)

    @api.model
    def _zivo_mark_converted(self, order):
        """Close the recovery lead once the same customer has ordered."""
        email = order.partner_id.email
        if not email:
            return
        leads = self.search([
            ("zivo_instance_id", "=", order.zivo_instance_id.id),
            ("zivo_checkout_gid", "!=", False),
            ("email_from", "=ilike", email),
            ("active", "=", True),
            ("won_status", "=", "pending"),
            ("create_date", ">=", fields.Datetime.now() - timedelta(days=30)),
        ])
        if leads:
            leads.action_set_won()
            for lead in leads:
                lead.message_post(body=self.env._("Recovered: converted to order %s.", order.name))

    def action_zivo_generate_email(self):
        for lead in self.filtered("zivo_checkout_gid"):
            lead.zivo_recovery_email = lead._zivo_draft_email()
        return True

    def _zivo_draft_email(self):
        self.ensure_one()
        store = self.zivo_instance_id
        first_name = (self.contact_name or self.partner_name or "").split(" ")[0] or self.env._("there")
        body = None
        if store._ai_available():
            prompt = (
                f"Store: {store.shop_name or store.name}\nCart:\n{self.zivo_cart_summary}\n"
                "Write a short, friendly cart recovery email body, 3 short paragraphs at most. "
                "Use the literal placeholder {first_name} for the customer name and {link} for the checkout link. "
                "Do not invent discounts. Plain text only."
            )
            try:
                body = store._ai_complete("recovery_email", "You write concise e-commerce emails.", prompt, max_tokens=400)
            except ai_lib.AIError as error:
                _logger.info("LLM unavailable, using template: %s", error)
        if not body:
            body = self.env._(
                "Hi {first_name},\n\nYou left a few items in your cart at %(store)s. We saved them for you:\n\n"
                "%(cart)s\n\nYou can finish your order here: {link}",
                store=store.shop_name or store.name, cart=self.zivo_cart_summary or "",
            )
        safe = escape(body.replace("{first_name}", first_name).replace("{link}", self.zivo_recovery_url or ""))
        return Markup("<p>%s</p>") % Markup("<br/>").join(escape(row) for row in str(safe).split("\n"))


class ZivoShopifyInstanceCommerce(models.Model):
    _inherit = "zivo.shopify.instance"

    def action_import_checkouts(self):
        self.ensure_one()
        self._enqueue("import_checkouts", "Import abandoned checkouts", {}, dedupe_key="import_checkouts")
        return self._notify(self.env._("Checkout import queued."), "success")

    def action_import_markets(self):
        self.ensure_one()
        self._enqueue("import_markets", "Import markets", {}, dedupe_key="import_markets")
        return self._notify(self.env._("Market import queued."), "success")

    def _job_import_checkouts(self, job):
        Lead = self.env["crm.lead"].with_company(self.company_id)
        since = self._shopify_since_query(self.last_checkout_sync) or None
        count = 0
        for node in self._client().iter_connection(Q.ABANDONED_CHECKOUTS, {"query": since}, ["abandonedCheckouts"], max_pages=20):
            if node.get("completedAt"):
                continue
            customer = node.get("customer") or {}
            email = (customer.get("defaultEmailAddress") or {}).get("emailAddress")
            if not email:
                continue
            lines = (node.get("lineItems") or {}).get("nodes") or []
            summary = "\n".join(
                f"{line['quantity']} x {line.get('title') or ''}"
                + (f" ({line['variantTitle']})" if line.get("variantTitle") else "")
                for line in lines
            )
            contact = f"{customer.get('firstName') or ''} {customer.get('lastName') or ''}".strip()
            vals = {
                "name": self.env._("Abandoned checkout %(name)s - %(contact)s", name=node["name"], contact=contact or email),
                "contact_name": contact,
                "email_from": email,
                "phone": (customer.get("defaultPhoneNumber") or {}).get("phoneNumber"),
                "expected_revenue": float(node["totalPriceSet"]["shopMoney"]["amount"]),
                "zivo_recovery_url": node.get("abandonedCheckoutUrl"),
                "zivo_cart_summary": summary,
            }
            lead = Lead.search([("zivo_instance_id", "=", self.id), ("zivo_checkout_gid", "=", node["id"])], limit=1)
            if lead:
                lead.write(vals)
            else:
                Lead.create({
                    **vals, "type": "opportunity", "zivo_instance_id": self.id, "zivo_checkout_gid": node["id"],
                    "team_id": self.team_id.id, "user_id": self.user_id.id, "company_id": self.company_id.id,
                })
            count += 1
        self.last_checkout_sync = fields.Datetime.now()
        return f"{count} checkouts"

    def _job_import_markets(self, job):
        Market = self.env["zivo.shopify.market"]
        count = 0
        for node in self._client().iter_connection(Q.MARKETS, {}, ["markets"], expected_cost=400):
            region = ((node.get("conditions") or {}).get("regionsCondition") or {}).get("regions") or {}
            codes = ",".join(r.get("code") for r in region.get("nodes") or [] if r.get("code"))
            catalog = next(iter((node.get("catalogs") or {}).get("nodes") or []), {})
            price_list = catalog.get("priceList") or {}
            vals = {
                "name": node["name"], "handle": node.get("handle"), "status": node.get("status"),
                "market_type": node.get("type"), "country_codes": codes,
                "price_list_gid": price_list.get("id"), "price_list_currency": price_list.get("currency"),
            }
            market = Market.search([("instance_id", "=", self.id), ("shopify_gid", "=", node["id"])], limit=1)
            if market:
                market.write(vals)
            else:
                Market.create({**vals, "instance_id": self.id, "shopify_gid": node["id"]})
            count += 1
        return f"{count} markets"

    def _job_export_market_prices(self, job):
        market = self.env["zivo.shopify.market"].browse(job.payload["market"]).exists()
        if not market.price_list_gid or not market.pricelist_id:
            return "Market needs a Shopify price list and an Odoo pricelist"
        client = self._client()
        variants = self.env["zivo.shopify.variant"].search([("instance_id", "=", self.id), ("product_id", "!=", False)])
        prices = [
            {"variantId": variant.shopify_gid, "price": {
                "amount": f"{market.pricelist_id._get_product_price(variant.product_id, 1.0):.2f}",
                "currencyCode": market.price_list_currency or market.pricelist_id.currency_id.name,
            }}
            for variant in variants
        ]
        for start in range(0, len(prices), 250):
            client.mutate(Q.PRICE_LIST_ADD, {"priceListId": market.price_list_gid, "prices": prices[start:start + 250]}, "priceListFixedPricesAdd")
        return f"{len(prices)} prices"
