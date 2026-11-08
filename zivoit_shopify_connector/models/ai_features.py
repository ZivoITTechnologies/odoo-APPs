import json
import logging
import math
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..lib import ai as ai_lib

_logger = logging.getLogger(__name__)


class ZivoShopifyAiLog(models.Model):
    _name = "zivo.shopify.ai.log"
    _description = "Shopify AI Usage"
    _order = "id desc"

    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    feature = fields.Char(required=True)
    model = fields.Char()
    state = fields.Selection([("ok", "Success"), ("error", "Error")], default="ok", required=True)
    message = fields.Char()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()


class ZivoShopifyMatch(models.Model):
    _name = "zivo.shopify.match"
    _description = "AI Product Match Suggestion"
    _order = "state desc, confidence desc, id desc"

    variant_id = fields.Many2one("zivo.shopify.variant", required=True, ondelete="cascade", index=True)
    instance_id = fields.Many2one(related="variant_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="variant_id.company_id", store=True, index=True)
    product_id = fields.Many2one("product.product", string="Suggested Odoo Product", required=True, ondelete="cascade")
    confidence = fields.Float(digits=(5, 4))
    confidence_percent = fields.Float(compute="_compute_percent", string="Confidence %")
    reason = fields.Char()
    engine = fields.Selection([("local", "Local Matcher"), ("llm", "LLM")], default="local", required=True)
    state = fields.Selection([("suggested", "Suggested"), ("accepted", "Accepted"), ("rejected", "Rejected")], default="suggested", required=True, index=True)

    _pair_unique = models.Constraint("UNIQUE(variant_id, product_id)", "Suggestion already exists.")

    @api.depends("confidence")
    def _compute_percent(self):
        for match in self:
            match.confidence_percent = match.confidence * 100

    @api.model
    def _candidates(self, variant):
        Product = self.env["product.product"]
        company = [("company_id", "in", [variant.company_id.id, False]), ("sale_ok", "=", True)]
        found = Product.browse()
        words = [word for word in (variant.shopify_product_id.name or "").split() if len(word) >= 3][:4]
        for word in words:
            found |= Product.search([("name", "ilike", word)] + company, limit=40)
        if variant.sku and len(variant.sku) >= 3:
            found |= Product.search([("default_code", "ilike", variant.sku[:6])] + company, limit=40)
        return found

    @api.model
    def _suggest_for(self, variants):
        """Score candidates locally, optionally let the LLM settle close calls."""
        for variant in variants.filtered(lambda v: not v.product_id):
            store = variant.instance_id
            title = variant.name or ""
            scored = []
            for product in self._candidates(variant):
                confidence, reason = ai_lib.score_product_match(
                    variant.sku, variant.barcode, title, product.default_code, product.barcode, product.display_name)
                if confidence >= 0.35:
                    scored.append((confidence, reason, product))
            scored.sort(key=lambda row: row[0], reverse=True)
            top = scored[:3]
            engine = "local"
            if top and 0.45 <= top[0][0] < store.ai_auto_match_threshold and store._ai_available():
                top, engine = self._llm_rerank(store, variant, top)
            existing = self.search([("variant_id", "=", variant.id)])
            for confidence, reason, product in top:
                match = existing.filtered(lambda m: m.product_id == product)
                if match:
                    if match.state == "suggested":
                        match.write({"confidence": confidence, "reason": reason, "engine": engine})
                else:
                    self.create({"variant_id": variant.id, "product_id": product.id, "confidence": confidence, "reason": reason, "engine": engine})
            if top and top[0][0] >= store.ai_auto_match_threshold:
                self.search([("variant_id", "=", variant.id), ("product_id", "=", top[0][2].id)], limit=1).action_accept()

    @api.model
    def _llm_rerank(self, store, variant, top):
        prompt = json.dumps({
            "shopify": {"title": variant.name, "sku": variant.sku},
            "candidates": [
                {"index": i, "name": row[2].display_name, "sku": row[2].default_code} for i, row in enumerate(top)
            ],
            "task": "Pick the candidate describing the same sellable product, or -1 when none does. "
                    "Answer as JSON: {\"index\": int, \"confidence\": 0..1, \"reason\": str}.",
        })
        try:
            answer = ai_lib.extract_json(store._ai_complete("product_match", "You reconcile e-commerce catalogs.", prompt, max_tokens=200)) or {}
            index = int(answer.get("index", -2))
        except (ai_lib.AIError, TypeError, ValueError) as error:
            _logger.info("Match rerank skipped: %s", error)
            return top, "local"
        if index == -1:
            return [(min(row[0], 0.4), row[1], row[2]) for row in top], "llm"
        if not 0 <= index < len(top):
            return top, "local"
        # A model opinion can never reach auto-link territory on its own.
        confidence = min(float(answer.get("confidence") or top[index][0]), 0.96)
        chosen = (confidence, str(answer.get("reason") or top[index][1])[:200], top[index][2])
        return [chosen] + [row for i, row in enumerate(top) if i != index], "llm"

    def action_accept(self):
        for match in self:
            match.variant_id.write({"product_id": match.product_id.id, "stock_dirty": True})
            shopify_product = match.variant_id.shopify_product_id
            if not shopify_product.product_tmpl_id:
                shopify_product.product_tmpl_id = match.product_id.product_tmpl_id
            match.state = "accepted"
            (self.search([("variant_id", "=", match.variant_id.id), ("state", "=", "suggested")]) - match).write({"state": "rejected"})
        return True

    def action_reject(self):
        self.write({"state": "rejected"})
        return True


class ZivoShopifyForecast(models.Model):
    _name = "zivo.shopify.forecast"
    _description = "AI Demand Forecast"
    _order = "days_of_cover, id"

    variant_id = fields.Many2one("zivo.shopify.variant", required=True, ondelete="cascade", index=True)
    instance_id = fields.Many2one(related="variant_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="variant_id.company_id", store=True, index=True)
    product_id = fields.Many2one(related="variant_id.product_id", store=True)
    daily_velocity = fields.Float(string="Units / Day", digits=(10, 3))
    trend_percent = fields.Float(string="Trend %", digits=(10, 1))
    stock_qty = fields.Float(string="Free Stock")
    days_of_cover = fields.Float(digits=(10, 1))
    stockout_date = fields.Date(string="Projected Stockout")
    suggested_reorder = fields.Integer()
    risk = fields.Selection([("critical", "Critical"), ("warning", "Warning"), ("ok", "Healthy"), ("idle", "No Demand")], required=True, default="idle", index=True)

    _variant_unique = models.Constraint("UNIQUE(variant_id)", "One forecast per variant.")

    @api.model
    def _ewma(self, series, alpha=0.25):
        """Exponentially weighted average: recent days count more than old ones."""
        value = 0.0
        for index, quantity in enumerate(series):
            value = quantity if index == 0 else alpha * quantity + (1 - alpha) * value
        return value

    @api.model
    def _cron_recompute(self):
        for instance in self.env["zivo.shopify.instance"].search([("state", "=", "connected")]):
            self._recompute(instance)

    @api.model
    def _recompute(self, instance, horizon_days=60, target_cover=30):
        today = fields.Date.context_today(self)
        start = today - timedelta(days=horizon_days - 1)
        Line = self.env["sale.order.line"]
        rows = Line._read_group(
            [("order_id.zivo_instance_id", "=", instance.id), ("state", "=", "sale"),
             ("order_id.date_order", ">=", fields.Datetime.to_datetime(start)), ("product_id", "!=", False)],
            ["product_id", "order_id.date_order:day"], ["product_uom_qty:sum"],
        )
        series = {}
        for product, day, quantity in rows:
            series.setdefault(product.id, {})[fields.Date.to_date(day)] = quantity
        variants = self.env["zivo.shopify.variant"].search([("instance_id", "=", instance.id), ("product_id", "!=", False)])
        existing = {f.variant_id.id: f for f in self.search([("instance_id", "=", instance.id)])}
        for variant in variants:
            days = series.get(variant.product_id.id, {})
            daily = [days.get(start + timedelta(days=i), 0.0) for i in range(horizon_days)]
            velocity = self._ewma(daily)
            recent, previous = sum(daily[-14:]), sum(daily[-28:-14])
            trend = ((recent - previous) / previous * 100) if previous else (100.0 if recent else 0.0)
            stock = variant.product_id.with_context(zivo_skip_stock_flag=True).free_qty
            cover = stock / velocity if velocity > 0.001 else 9999.0
            if velocity <= 0.001:
                risk = "idle"
            elif cover <= 7:
                risk = "critical"
            elif cover <= 21:
                risk = "warning"
            else:
                risk = "ok"
            vals = {
                "daily_velocity": velocity,
                "trend_percent": trend,
                "stock_qty": stock,
                "days_of_cover": min(cover, 9999.0),
                "stockout_date": today + timedelta(days=int(cover)) if cover < 3650 else False,
                "suggested_reorder": max(math.ceil(velocity * target_cover - stock), 0) if velocity > 0.001 else 0,
                "risk": risk,
            }
            record = existing.get(variant.id)
            if record:
                record.write(vals)
            else:
                self.create({**vals, "variant_id": variant.id})


class ZivoShopifyRouting(models.Model):
    _inherit = "zivo.shopify.routing.rule"

    @api.model
    def _zivo_route_order(self, instance, market, shipping, demand):
        """Pick a warehouse: geography proposes, live stock decides. Returns ``(warehouse, note)``."""
        country = shipping.get("countryCodeV2")
        state = shipping.get("provinceCode")
        zip_code = shipping.get("zip")
        candidates = []
        for rule in instance.routing_rule_ids.filtered("active"):
            if rule._matches(country, state, zip_code) and rule.warehouse_id not in candidates:
                candidates.append(rule.warehouse_id)
        matched_rules = len(candidates)
        if market.warehouse_id and market.warehouse_id not in candidates:
            candidates.append(market.warehouse_id)
        if instance.warehouse_id and instance.warehouse_id not in candidates:
            candidates.append(instance.warehouse_id)
        for location in instance.location_ids:
            for warehouse in location.warehouse_ids:
                if warehouse not in candidates:
                    candidates.append(warehouse)
        if not candidates:
            return self.env["stock.warehouse"].search([("company_id", "=", instance.company_id.id)], limit=1), "Company default"
        if not matched_rules and len(candidates) == 1:
            return candidates[0], "Store default warehouse"

        Product = self.env["product.product"].with_context(zivo_skip_stock_flag=True)
        best, best_cover = candidates[0], -1.0
        total = sum(demand.values()) or 1.0
        for warehouse in candidates:
            covered = 0.0
            for product_id, quantity in demand.items():
                product = Product.browse(product_id)
                if not product.is_storable:
                    covered += quantity
                    continue
                available = product.with_context(warehouse_id=warehouse.id).free_qty
                covered += min(max(available, 0.0), quantity)
            cover = covered / total
            if cover >= 0.9999:
                preferred = "rule match" if candidates.index(warehouse) < matched_rules else "fallback"
                return warehouse, f"{warehouse.name}: full stock ({preferred})"
            if cover > best_cover:
                best, best_cover = warehouse, cover
        return best, f"{best.name}: best partial stock ({round(best_cover * 100)}%)"

    @api.model
    def _zivo_split_lines(self, instance, order, candidates):
        """Give each line the first warehouse able to serve it, keeping quantities together."""
        Product = self.env["product.product"].with_context(zivo_skip_stock_flag=True)
        remaining = {}
        moved = set()
        for line in order.order_line.filtered(lambda l: l.product_id.is_storable and l.product_uom_qty > 0):
            for warehouse in candidates:
                key = (warehouse.id, line.product_id.id)
                if key not in remaining:
                    remaining[key] = Product.browse(line.product_id.id).with_context(warehouse_id=warehouse.id).free_qty
                if remaining[key] >= line.product_uom_qty:
                    remaining[key] -= line.product_uom_qty
                    if warehouse != order.warehouse_id:
                        line.warehouse_id = warehouse
                    moved.add(warehouse.name)
                    break
        return sorted(moved)

    @api.model
    def _zivo_candidates(self, instance):
        result = []
        for rule in instance.routing_rule_ids.filtered("active"):
            if rule.warehouse_id not in result:
                result.append(rule.warehouse_id)
        for warehouse in instance.warehouse_id | instance.location_ids.warehouse_ids:
            if warehouse not in result:
                result.append(warehouse)
        return result


class ZivoShopifyContentWizard(models.TransientModel):
    _name = "zivo.shopify.content.wizard"
    _description = "AI Product Content Studio"

    shopify_product_id = fields.Many2one("zivo.shopify.product", required=True)
    tone = fields.Selection([
        ("professional", "Professional"), ("friendly", "Friendly"), ("luxury", "Premium"), ("technical", "Technical"),
    ], default="professional", required=True)
    language = fields.Char(default="English", required=True)
    keywords = fields.Char(string="SEO Keywords")
    instructions = fields.Text()
    title = fields.Char()
    description_html = fields.Html(string="Description", sanitize=True)
    seo_title = fields.Char(string="SEO Title")
    seo_description = fields.Text(string="SEO Description")
    tags = fields.Char()
    generated = fields.Boolean()
    engine = fields.Char(readonly=True)
    push_after = fields.Boolean(string="Export to Shopify after applying", default=True)

    def _facts(self):
        self.ensure_one()
        product = self.shopify_product_id
        template = product.product_tmpl_id
        attributes = {
            line.attribute_id.name: line.value_ids.mapped("name") for line in template.attribute_line_ids
        } if template else {}
        return {
            "name": product.name,
            "category": template.categ_id.name if template else product.product_type,
            "vendor": product.vendor,
            "existing_description": (template.description_sale if template else "") or "",
            "attributes": attributes,
            "weight": template.weight if template else 0,
        }

    def action_generate(self):
        self.ensure_one()
        store = self.shopify_product_id.instance_id
        facts = self._facts()
        if store._ai_available():
            system = (
                "You write accurate e-commerce product copy. Use only the supplied facts and never invent "
                "specifications, materials, certifications or claims. Answer in JSON with keys: title, "
                "description_html (simple <p> and <ul> only), seo_title (max 70 characters), "
                "seo_description (max 160 characters), tags (array of up to 8 strings)."
            )
            prompt = json.dumps({
                "facts": facts, "tone": self.tone, "language": self.language,
                "keywords": self.keywords or "", "instructions": self.instructions or "",
            })
            answer = ai_lib.extract_json(store._ai_complete("content_studio", system, prompt, max_tokens=1200))
            if not isinstance(answer, dict):
                raise UserError(self.env._("The AI answer could not be read. Please try again."))
            self.write({
                "title": answer.get("title") or facts["name"],
                "description_html": answer.get("description_html") or "",
                "seo_title": (answer.get("seo_title") or "")[:70],
                "seo_description": (answer.get("seo_description") or "")[:160],
                "tags": ", ".join(answer.get("tags") or []),
                "generated": True,
                "engine": self.env._("LLM"),
            })
        else:
            self._generate_local(facts)
        return self._reopen()

    def _generate_local(self, facts):
        specs = "".join(f"<li>{name}: {', '.join(values)}</li>" for name, values in facts["attributes"].items())
        intro = facts["existing_description"] or self.env._("%s, available now.", facts["name"])
        description = f"<p>{intro}</p>" + (f"<ul>{specs}</ul>" if specs else "")
        self.write({
            "title": facts["name"],
            "description_html": description,
            "seo_title": facts["name"][:70],
            "seo_description": intro[:160],
            "tags": ", ".join(filter(None, [facts["category"], facts["vendor"]] + [k.strip() for k in (self.keywords or "").split(",")])),
            "generated": True,
            "engine": self.env._("Local Template (no LLM configured)"),
        })

    def _reopen(self):
        return {"name": self.env._("AI Content Studio"), "type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form", "target": "new"}

    def action_apply(self):
        self.ensure_one()
        product = self.shopify_product_id
        product.write({
            "name": self.title or product.name,
            "description_html": self.description_html,
            "seo_title": self.seo_title,
            "seo_description": self.seo_description,
            "tags": self.tags,
        })
        if self.push_after:
            product.action_export()
        return {"type": "ir.actions.act_window_close"}
