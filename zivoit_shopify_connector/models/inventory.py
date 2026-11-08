import logging
import math
import uuid
from datetime import timedelta

from odoo import api, fields, models

from ..lib import queries as Q
from ..lib.client import ShopifyError, ShopifyUserError

_logger = logging.getLogger(__name__)

BATCH_SIZE = 200


class ZivoShopifyLocation(models.Model):
    _name = "zivo.shopify.location"
    _description = "Shopify Location"
    _order = "name"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(string="Shopify ID", required=True, index=True, readonly=True)
    warehouse_ids = fields.Many2many("stock.warehouse", string="Odoo Warehouses",
                                     help="Their combined stock is exported to this location.")
    import_warehouse_id = fields.Many2one("stock.warehouse", string="Import Into", help="Warehouse updated by Import Stock.")
    export_stock = fields.Boolean(default=True)
    city = fields.Char(readonly=True)
    zip = fields.Char(readonly=True)
    country_code = fields.Char(readonly=True)
    active_on_shopify = fields.Boolean(readonly=True, default=True)
    fulfills_online = fields.Boolean(readonly=True, default=True)
    is_fulfillment_service = fields.Boolean(readonly=True)

    _gid_unique = models.Constraint("UNIQUE(instance_id, shopify_gid)", "Location already imported.")


class ZivoShopifyVariantLevel(models.Model):
    _name = "zivo.shopify.variant.level"
    _description = "Shopify Stock Level"

    variant_id = fields.Many2one("zivo.shopify.variant", required=True, ondelete="cascade", index=True)
    location_id = fields.Many2one("zivo.shopify.location", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="location_id.company_id", store=True, index=True)
    last_exported_qty = fields.Integer(default=-1, readonly=True)
    shopify_available = fields.Integer(readonly=True)
    shopify_seen_at = fields.Datetime(readonly=True)

    _pair_unique = models.Constraint("UNIQUE(variant_id, location_id)", "Level already exists.")

    @api.model
    def _get_or_create(self, variant, location):
        level = self.search([("variant_id", "=", variant.id), ("location_id", "=", location.id)], limit=1)
        return level or self.create({"variant_id": variant.id, "location_id": location.id})

    @api.model
    def _apply_webhook(self, instance, body):
        item = f"gid://shopify/InventoryItem/{body.get('inventory_item_id')}"
        location_gid = f"gid://shopify/Location/{body.get('location_id')}"
        variant = self.env["zivo.shopify.variant"].search(
            [("instance_id", "=", instance.id), ("inventory_item_gid", "=", item)], limit=1)
        location = self.env["zivo.shopify.location"].search(
            [("instance_id", "=", instance.id), ("shopify_gid", "=", location_gid)], limit=1)
        if variant and location:
            self._get_or_create(variant, location).write({
                "shopify_available": body.get("available") or 0,
                "shopify_seen_at": fields.Datetime.now(),
            })


class ZivoShopifyDrift(models.Model):
    _name = "zivo.shopify.drift"
    _description = "Shopify Stock Drift"
    _order = "abs_difference desc, id desc"

    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    variant_id = fields.Many2one("zivo.shopify.variant", required=True, ondelete="cascade")
    product_id = fields.Many2one(related="variant_id.product_id", store=True)
    location_id = fields.Many2one("zivo.shopify.location", required=True, ondelete="cascade")
    odoo_qty = fields.Integer(string="Expected (Odoo)")
    shopify_qty = fields.Integer(string="On Shopify")
    difference = fields.Integer()
    abs_difference = fields.Integer(index=True)
    state = fields.Selection([("open", "Open"), ("fixed", "Corrected"), ("ignored", "Ignored")], default="open", required=True, index=True)
    detected_at = fields.Datetime(default=fields.Datetime.now)

    def action_fix(self):
        for instance in self.instance_id:
            drifts = self.filtered(lambda d: d.instance_id == instance and d.state == "open")
            drifts.variant_id.write({"stock_dirty": True})
            drifts.variant_id.level_ids.write({"last_exported_qty": -1})
            instance._export_stock(drifts.variant_id)
            drifts.write({"state": "fixed"})
        return True

    def action_ignore(self):
        self.write({"state": "ignored"})
        return True


class StockMove(models.Model):
    _inherit = "stock.move"

    def _zivo_flag_stock(self):
        if self.env.context.get("zivo_skip_stock_flag"):
            return
        products = self.product_id
        if not products:
            return
        # Kits: a component move changes the sellable quantity of its parents.
        if "mrp.bom.line" in self.env:
            parents = self.env["mrp.bom.line"].sudo().search(
                [("product_id", "in", products.ids), ("bom_id.type", "=", "phantom")]).bom_id
            products |= parents.product_id | parents.product_tmpl_id.product_variant_ids
        variants = self.env["zivo.shopify.variant"].sudo().search([
            ("product_id", "in", products.ids), ("stock_dirty", "=", False),
        ])
        if variants:
            variants.write({"stock_dirty": True})
            cron = self.env.ref("zivoit_shopify_connector.cron_export_stock", raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger(fields.Datetime.now() + timedelta(seconds=60))

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        moves._zivo_flag_stock()
        return moves

    def _action_assign(self, force_qty=False):
        result = super()._action_assign(force_qty=force_qty)
        self._zivo_flag_stock()
        return result


class ZivoShopifyInstanceStock(models.Model):
    _inherit = "zivo.shopify.instance"

    def _variant_stock(self, variant, location):
        """Quantity to publish for a variant at a location, after every rule."""
        self.ensure_one()
        warehouses = location.warehouse_ids
        if variant.stock_policy == "exclude" or not warehouses or not variant.product_id:
            return None
        if variant.stock_policy == "fixed":
            return max(int(variant.stock_policy_value), 0)
        companies = warehouses.company_id
        product = variant.product_id.with_context(
            warehouse_id=warehouses.ids, allowed_company_ids=companies.ids, zivo_skip_stock_flag=True,
        ).sudo()
        quantity = {
            "free": product.free_qty,
            "on_hand": product.qty_available,
            "forecast": product.virtual_available,
        }[self.stock_source]
        percent = variant.stock_policy_value if variant.stock_policy == "percent" else self.stock_percent
        quantity = quantity * percent / 100.0
        if self.smart_buffer_enabled and variant.stock_policy == "default":
            forecast = self.env["zivo.shopify.forecast"].search([("variant_id", "=", variant.id)], limit=1)
            quantity -= math.ceil((forecast.daily_velocity or 0.0) * self.smart_buffer_hours / 24.0)
        return max(int(math.floor(quantity)), 0)

    def _export_stock(self, variants, force=False):
        """Publish absolute quantities; a repeated run converges to the same state."""
        self.ensure_one()
        Level = self.env["zivo.shopify.variant.level"]
        locations = self.location_ids.filtered(lambda l: l.export_stock and l.warehouse_ids and l.active_on_shopify)
        quantities, levels = [], []
        for variant in variants.filtered(lambda v: v.inventory_item_gid and v.tracked and v.product_id):
            for location in locations:
                qty = self._variant_stock(variant, location)
                if qty is None:
                    continue
                level = Level._get_or_create(variant, location)
                if not force and level.last_exported_qty == qty:
                    continue
                quantities.append({
                    "inventoryItemId": variant.inventory_item_gid,
                    "locationId": location.shopify_gid,
                    "quantity": qty,
                    "changeFromQuantity": None,
                })
                levels.append((level, qty))
        if quantities:
            client = self._client()
            for start in range(0, len(quantities), BATCH_SIZE):
                batch = quantities[start:start + BATCH_SIZE]
                self._send_stock_batch(client, batch)
            for level, qty in levels:
                level.write({"last_exported_qty": qty, "shopify_available": qty, "shopify_seen_at": fields.Datetime.now()})
        variants.write({"stock_dirty": False, "last_stock_export_at": fields.Datetime.now()})
        return len(quantities)

    def _send_stock_batch(self, client, batch):
        variables = {
            "input": {"name": "available", "reason": "correction", "quantities": batch},
            "key": uuid.uuid4().hex,
        }
        try:
            client.mutate(Q.INVENTORY_SET, variables, "inventorySetQuantities")
        except ShopifyUserError as error:
            message = str(error).lower()
            if "not stocked" not in message and "activat" not in message and error.code != "INVALID_LOCATION":
                raise
            for line in batch:
                client.mutate(Q.INVENTORY_ACTIVATE, {
                    "inventoryItemId": line["inventoryItemId"],
                    "locationId": line["locationId"],
                    "key": uuid.uuid4().hex,
                }, "inventoryActivate")
            client.mutate(Q.INVENTORY_SET, {**variables, "key": uuid.uuid4().hex}, "inventorySetQuantities")

    @api.model
    def _cron_export_stock(self):
        Variant = self.env["zivo.shopify.variant"]
        for instance in self._connected().filtered("auto_export_stock"):
            dirty = Variant.search([("instance_id", "=", instance.id), ("stock_dirty", "=", True), ("product_id", "!=", False)], limit=2000)
            for start in range(0, len(dirty), BATCH_SIZE):
                ids = dirty[start:start + BATCH_SIZE].ids
                instance._enqueue("export_stock", f"Export stock of {len(ids)} variants",
                                  {"variants": ids}, source="odoo", priority=6)

    def action_export_all_stock(self):
        self.ensure_one()
        ids = self.env["zivo.shopify.variant"].search([("instance_id", "=", self.id), ("product_id", "!=", False)]).ids
        for start in range(0, len(ids), BATCH_SIZE):
            self._enqueue("export_stock", f"Full stock export {start + 1}+", {"variants": ids[start:start + BATCH_SIZE], "force": True})
        return self._notify(self.env._("Full stock export queued."), "success")

    def action_import_stock(self):
        self.ensure_one()
        for location in self.location_ids.filtered("import_warehouse_id"):
            self._enqueue("import_stock", f"Import stock from {location.name}", {"location": location.id})
        return self._notify(self.env._("Stock import queued."), "success")

    def _job_export_stock(self, job):
        variants = self.env["zivo.shopify.variant"].browse(job.payload["variants"]).exists()
        return f"{self._export_stock(variants, force=job.payload.get('force'))} quantities sent"

    def _job_import_stock(self, job):
        location = self.env["zivo.shopify.location"].browse(job.payload["location"])
        client = self._client()
        Variant = self.env["zivo.shopify.variant"]
        Quant = self.env["stock.quant"].with_context(inventory_mode=True, zivo_skip_stock_flag=True)
        target = location.import_warehouse_id.lot_stock_id
        count = 0
        for node in client.iter_connection(Q.INVENTORY_LEVELS, {"location": location.shopify_gid}, ["location", "inventoryLevels"]):
            variant = Variant.search([("instance_id", "=", self.id), ("inventory_item_gid", "=", node["item"]["id"]), ("product_id", "!=", False)], limit=1)
            if not variant or not variant.product_id.is_storable:
                continue
            quantities = {q["name"]: q["quantity"] for q in node["quantities"]}
            on_hand = quantities.get("on_hand", quantities.get("available", 0))
            quant = Quant.search([("product_id", "=", variant.product_id.id), ("location_id", "=", target.id)], limit=1)
            if quant:
                quant.inventory_quantity = on_hand
            else:
                quant = Quant.create({"product_id": variant.product_id.id, "location_id": target.id, "inventory_quantity": on_hand})
            quant.action_apply_inventory()
            count += 1
        return f"{count} products adjusted"

    # ------------------------------------------------------------------
    # Drift Guard
    # ------------------------------------------------------------------
    @api.model
    def _cron_drift_guard(self):
        for instance in self._connected().filtered("drift_guard_enabled"):
            # One unreachable store must not stop the check of the others.
            try:
                with self.env.cr.savepoint():
                    instance._scan_drift()
            except ShopifyError as error:
                _logger.warning("Drift Guard skipped store %s: %s", instance.name, error)

    def _scan_drift(self):
        """Compare what Shopify shows with what the rules say it should show."""
        self.ensure_one()
        Drift = self.env["zivo.shopify.drift"]
        Variant = self.env["zivo.shopify.variant"]
        client = self._client()
        Drift.search([("instance_id", "=", self.id), ("state", "=", "open")]).unlink()
        found = Drift
        for location in self.location_ids.filtered(lambda l: l.export_stock and l.warehouse_ids):
            levels = client.iter_connection(Q.INVENTORY_LEVELS, {"location": location.shopify_gid}, ["location", "inventoryLevels"])
            for node in levels:
                variant = Variant.search([("instance_id", "=", self.id), ("inventory_item_gid", "=", node["item"]["id"]), ("product_id", "!=", False)], limit=1)
                if not variant or variant.stock_dirty:
                    continue
                expected = self._variant_stock(variant, location)
                if expected is None:
                    continue
                available = next((q["quantity"] for q in node["quantities"] if q["name"] == "available"), 0)
                if available != expected:
                    found |= Drift.create({
                        "instance_id": self.id, "variant_id": variant.id, "location_id": location.id,
                        "odoo_qty": expected, "shopify_qty": available,
                        "difference": available - expected, "abs_difference": abs(available - expected),
                    })
        if self.drift_auto_fix and found:
            found.action_fix()
        return found

    def action_scan_drift(self):
        self.ensure_one()
        found = self._scan_drift()
        return self._notify(self.env._("Drift Guard found %s difference(s).", len(found)), "warning" if found else "success")
