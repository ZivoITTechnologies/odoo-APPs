import logging

from odoo import api, fields, models

from ..lib import ai as ai_lib, queries as Q

_logger = logging.getLogger(__name__)

ROOT_CATEGORY = "Shopify Collections"
CATEGORY_MATCH_CONFIDENCE = 0.85


class ZivoShopifyCollection(models.Model):
    _name = "zivo.shopify.collection"
    _description = "Shopify Collection"
    _order = "sequence, id"

    # Odoo 19 categories have untranslatable names, so collection titles are translated here.
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10, help="When a product is in several collections, the first one sets its category.")
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(string="Shopify ID", required=True, index=True, readonly=True, copy=False)
    handle = fields.Char(readonly=True)
    collection_type = fields.Selection([("manual", "Manual"), ("smart", "Automated")], default="manual", required=True, string="Type")
    category_id = fields.Many2one("product.category", string="Odoo Category", ondelete="set null", index=True)
    category_created = fields.Boolean(readonly=True, copy=False)
    match_note = fields.Char(string="Mapping", readonly=True)

    _gid_unique = models.Constraint("UNIQUE(instance_id, shopify_gid)", "Collection already imported.")

    @api.model
    def _upsert_from_node(self, instance, node):
        self = self.with_context(lang=instance._source_lang())
        record = self.search([("instance_id", "=", instance.id), ("shopify_gid", "=", node["id"])], limit=1)
        smart = any(source.get("__typename") == "CollectionConditionsSource" for source in node.get("sources") or [])
        vals = {"name": node["title"], "handle": node.get("handle"), "collection_type": "smart" if smart else "manual"}
        if record:
            record.write(vals)
        else:
            # Automated collections such as "All products" say little about what a product is.
            record = self.create({**vals, "instance_id": instance.id, "shopify_gid": node["id"], "sequence": 50 if smart else 10})
        if not record.category_id:
            record._assign_category()
        return record

    def _assign_category(self):
        """Reuse the closest existing category, otherwise create one."""
        self.ensure_one()
        Category = self.env["product.category"]
        best, best_score = Category, 0.0
        for category in Category.search([]):
            score = ai_lib.similarity(self.name, category.name or "")
            if score > best_score:
                best, best_score = category, score
        if best and best_score >= CATEGORY_MATCH_CONFIDENCE:
            self.write({"category_id": best.id, "match_note": self.env._("Matched existing category (%s%%)", round(best_score * 100))})
            return
        if not self.instance_id.auto_create_categories:
            self.match_note = self.env._("No similar category found")
            return
        root = Category.search([("name", "=", ROOT_CATEGORY), ("parent_id", "=", False)], limit=1) or Category.create({"name": ROOT_CATEGORY})
        category = Category.search([("name", "=", self.name), ("parent_id", "=", root.id)], limit=1) or Category.create({"name": self.name, "parent_id": root.id})
        self.write({"category_id": category.id, "category_created": True, "match_note": self.env._("Created by the connector")})


class ZivoShopifyProductCollections(models.Model):
    _inherit = "zivo.shopify.product"

    def _apply_collections(self, node):
        """Record memberships and set the Odoo category from the highest ranked collection."""
        self.ensure_one()
        Collection = self.env["zivo.shopify.collection"]
        records = Collection
        for item in (node.get("collections") or {}).get("nodes") or []:
            records |= Collection._upsert_from_node(self.instance_id, item)
        self.collection_ids = [fields.Command.set(records.ids)]
        mapped = records.filtered("category_id").sorted("sequence")[:1]
        template = self.product_tmpl_id
        if mapped and template and (self.created_by_import or self.instance_id.update_existing_products):
            if template.categ_id != mapped.category_id:
                template.categ_id = mapped.category_id

    def _collections_for_export(self):
        """Manual collections only: Shopify computes membership of automated ones itself."""
        self.ensure_one()
        Collection = self.env["zivo.shopify.collection"]
        gids = self.collection_ids.filtered(lambda c: c.collection_type == "manual").mapped("shopify_gid")
        category = self.product_tmpl_id.categ_id
        if category:
            mapped = Collection.search([
                ("instance_id", "=", self.instance_id.id), ("category_id", "=", category.id), ("collection_type", "=", "manual"),
            ], limit=1)
            if not mapped and self.instance_id.export_categories:
                mapped = self._create_collection_from_category(category)
            if mapped and mapped.shopify_gid not in gids:
                gids.append(mapped.shopify_gid)
        return gids

    def _create_collection_from_category(self, category):
        self.ensure_one()
        payload = self.instance_id._client().mutate(Q.COLLECTION_CREATE, {"collection": {"title": category.name}}, "collectionCreate")
        node = payload["collection"]
        record = self.env["zivo.shopify.collection"].create({
            "instance_id": self.instance_id.id, "shopify_gid": node["id"], "name": node["title"], "handle": node.get("handle"),
            "collection_type": "manual", "category_id": category.id, "match_note": self.env._("Created on Shopify from this category"),
        })
        return record


class ZivoShopifyInstanceCollections(models.Model):
    _inherit = "zivo.shopify.instance"

    def action_import_collections(self):
        self.ensure_one()
        self._enqueue("import_collections", "Import collections", {}, dedupe_key="import_collections")
        return self._notify(self.env._("Collection import queued."), "success")

    def _job_import_collections(self, job):
        Collection = self.env["zivo.shopify.collection"]
        count = 0
        for node in self._client().iter_connection(Q.COLLECTIONS, {}, ["collections"], expected_cost=300):
            Collection._upsert_from_node(self, node)
            count += 1
        self._queue_collection_translations()
        return f"{count} collections"
