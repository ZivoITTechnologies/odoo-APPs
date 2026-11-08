import base64
import hashlib
import hmac
import logging
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools import html2plaintext

from ..lib import queries as Q

_logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 12 * 1024 * 1024
DEFAULT_TITLE = "Default Title"


class ZivoShopifyProduct(models.Model):
    _name = "zivo.shopify.product"
    _description = "Shopify Product"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(required=True, tracking=True, translate=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    product_tmpl_id = fields.Many2one("product.template", string="Odoo Product", index=True, ondelete="set null")
    shopify_gid = fields.Char(string="Shopify ID", index=True, copy=False)
    handle = fields.Char()
    status = fields.Selection([
        ("ACTIVE", "Active"), ("DRAFT", "Draft"), ("ARCHIVED", "Archived"), ("UNLISTED", "Unlisted"),
    ], default="DRAFT", tracking=True)
    vendor = fields.Char()
    product_type = fields.Char()
    tags = fields.Char()
    description_html = fields.Html(sanitize=True, translate=True)
    seo_title = fields.Char(string="SEO Title", translate=True)
    seo_description = fields.Text(string="SEO Description", translate=True)
    created_by_import = fields.Boolean(readonly=True, copy=False, help="Odoo product created by the connector.")
    option_data = fields.Json(help="Shopify option and value IDs, needed for their translations.")
    collection_ids = fields.Many2many("zivo.shopify.collection", string="Collections")
    image_url = fields.Char()
    excluded = fields.Boolean(string="Exclude from Sync", tracking=True)
    last_import_at = fields.Datetime(readonly=True)
    last_export_at = fields.Datetime(readonly=True)
    metafields = fields.Json()
    variant_ids = fields.One2many("zivo.shopify.variant", "shopify_product_id", string="Variants")
    variant_count = fields.Integer(compute="_compute_variant_count")
    unmatched_count = fields.Integer(compute="_compute_variant_count")

    _gid_unique = models.Constraint("UNIQUE(instance_id, shopify_gid)", "Product already linked on this store.")

    @api.depends("variant_ids.product_id")
    def _compute_variant_count(self):
        for product in self:
            product.variant_count = len(product.variant_ids)
            product.unmatched_count = len(product.variant_ids.filtered(lambda v: not v.product_id))

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    @api.model
    def _upsert_from_node(self, instance, node):
        """Create or refresh the mapping of one Shopify product and its variants."""
        self = self.with_context(lang=instance._source_lang())
        record = self.search([("instance_id", "=", instance.id), ("shopify_gid", "=", node["id"])], limit=1)
        if record.excluded:
            return record
        media = [m for m in (node.get("media") or {}).get("nodes") or [] if m.get("image")]
        seo = node.get("seo") or {}
        vals = {
            "name": node["title"],
            "handle": node.get("handle"),
            "status": node.get("status"),
            "vendor": node.get("vendor"),
            "product_type": node.get("productType"),
            "tags": ", ".join(node.get("tags") or []),
            "description_html": node.get("descriptionHtml"),
            "seo_title": seo.get("title"),
            "seo_description": seo.get("description"),
            "image_url": media[0]["image"]["url"] if media else False,
            "metafields": (node.get("metafields") or {}).get("nodes") or [],
            "option_data": node.get("options") or [],
            "last_import_at": fields.Datetime.now(),
        }
        if record:
            record.write(vals)
        else:
            vals.update(instance_id=instance.id, shopify_gid=node["id"])
            record = self.create(vals)

        variants = list((node.get("variants") or {}).get("nodes") or [])
        page = (node.get("variants") or {}).get("pageInfo") or {}
        if page.get("hasNextPage"):
            variants += list(instance._client().iter_connection(
                Q.PRODUCT_VARIANTS_PAGE, {"id": node["id"], "cursor": page.get("endCursor")},
                ["product", "variants"]))
        record._sync_variants(variants, node)
        self.env["zivo.shopify.metafield.mapping"]._apply_import(
            instance, "product", record.product_tmpl_id, vals["metafields"])
        record._apply_collections(node)
        record._suggest_matches()
        record._queue_translation_import()
        return record

    def _sync_variants(self, variant_nodes, product_node):
        self.ensure_one()
        Variant = self.env["zivo.shopify.variant"]
        seen = Variant
        for node in variant_nodes:
            variant = Variant.search([("instance_id", "=", self.instance_id.id), ("shopify_gid", "=", node["id"])], limit=1)
            vals = Variant._vals_from_node(node)
            if variant:
                variant.write(vals)
            else:
                vals.update(shopify_product_id=self.id, shopify_gid=node["id"])
                variant = Variant.create(vals)
            seen |= variant
        (self.variant_ids - seen).unlink()
        self._link_variants(product_node)

    def _link_variants(self, product_node):
        """Find the Odoo product of every variant; create them when configured."""
        self.ensure_one()
        instance = self.instance_id
        for variant in self.variant_ids.filtered(lambda v: not v.product_id):
            variant.product_id = variant._find_odoo_product()
        linked = self.variant_ids.product_id
        if linked and not self.product_tmpl_id:
            self.product_tmpl_id = linked[0].product_tmpl_id
        created_here = False
        missing = self.variant_ids.filtered(lambda v: not v.product_id)
        if missing and instance.auto_create_products and not linked:
            self._create_odoo_template(product_node)
            created_here = True
        elif self.product_tmpl_id and (created_here or instance.update_existing_products):
            self.product_tmpl_id.write(self._odoo_template_vals())
        if created_here and instance.sync_images and self.image_url and self.product_tmpl_id:
            self.product_tmpl_id.image_1920 = self._download_image(self.image_url)

    def _odoo_template_vals(self):
        self.ensure_one()
        return {"name": self.name, "description_sale": html2plaintext(self.description_html or "")[:2000]}

    def _create_odoo_template(self, product_node):
        self.ensure_one()
        options = [o for o in product_node.get("options") or [] if not (len(o.get("values") or []) == 1 and o["values"][0] == DEFAULT_TITLE)]
        Attribute = self.env["product.attribute"]
        attribute_lines = []
        for option in options:
            attribute = Attribute.search([("name", "=ilike", option["name"])], limit=1) or Attribute.create({
                "name": option["name"], "create_variant": "always",
            })
            value_ids = []
            for value in option["values"]:
                existing = attribute.value_ids.filtered(lambda v: v.name.lower() == value.lower())[:1]
                if not existing:
                    existing = self.env["product.attribute.value"].create({"attribute_id": attribute.id, "name": value})
                value_ids.append(existing.id)
            attribute_lines.append(Command.create({"attribute_id": attribute.id, "value_ids": [Command.set(value_ids)]}))
        first = self.variant_ids.sorted("position")[:1]
        template = self.env["product.template"].create({
            **self._odoo_template_vals(),
            "type": "consu",
            "is_storable": True,
            "invoice_policy": "order",
            "list_price": min(self.variant_ids.mapped("price") or [0.0]),
            "standard_price": first.unit_cost,
            "default_code": first.sku if len(self.variant_ids) == 1 else False,
            "barcode": first.barcode if len(self.variant_ids) == 1 else False,
            "attribute_line_ids": attribute_lines,
        })
        self.write({"product_tmpl_id": template.id, "created_by_import": True})
        for variant in self.variant_ids:
            product = variant._match_in_template(template)
            if product:
                variant.product_id = product
                write = {"standard_price": variant.unit_cost}
                if variant.sku and product.default_code != variant.sku:
                    write["default_code"] = variant.sku
                if variant.barcode and product.barcode != variant.barcode:
                    write["barcode"] = variant.barcode
                if write:
                    product.write(write)

    def _download_image(self, url):
        try:
            response = requests.get(url, timeout=20)
            if response.status_code == 200 and len(response.content) <= MAX_IMAGE_BYTES:
                return base64.b64encode(response.content)
        except requests.RequestException as error:
            _logger.info("Shopify image not downloaded: %s", error)
        return False

    def _suggest_matches(self):
        self.ensure_one()
        unmatched = self.variant_ids.filtered(lambda v: not v.product_id)
        if unmatched:
            self.env["zivo.shopify.match"]._suggest_for(unmatched)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    @api.model
    def _ensure_for_template(self, instance, template):
        record = self.search([("instance_id", "=", instance.id), ("product_tmpl_id", "=", template.id)], limit=1)
        if record:
            return record
        return self.create({
            "instance_id": instance.id,
            "product_tmpl_id": template.id,
            "name": template.name,
            "status": "DRAFT" if instance.export_as_draft else "ACTIVE",
            "description_html": template.description_sale and f"<p>{template.description_sale}</p>",
            "vendor": template.company_id.name or instance.company_id.name,
            "product_type": template.categ_id.name,
        })

    def _option_lines(self):
        self.ensure_one()
        return self.product_tmpl_id.valid_product_template_attribute_line_ids._without_no_variant_attributes()

    def _variant_price(self, product):
        instance = self.instance_id
        if instance.price_pricelist_id:
            return instance.price_pricelist_id._get_product_price(product, 1.0)
        return product.lst_price

    def _build_product_set_input(self):
        self.ensure_one()
        instance = self.instance_id
        template = self.product_tmpl_id
        if not template:
            raise UserError(self.env._("Shopify product %s has no Odoo product.", self.name))
        Mapping = self.env["zivo.shopify.metafield.mapping"]
        lines = self._option_lines()
        if lines:
            options = [
                {"name": line.attribute_id.name, "position": index + 1,
                 "values": [{"name": name} for name in line.product_template_value_ids.mapped("name")]}
                for index, line in enumerate(lines)
            ]
        else:
            options = [{"name": "Title", "position": 1, "values": [{"name": DEFAULT_TITLE}]}]
        variants = []
        for product in template.product_variant_ids:
            mapping = self.variant_ids.filtered(lambda v: v.product_id == product)[:1]
            if lines:
                option_values = [
                    {"optionName": value.attribute_id.name, "name": value.name}
                    for value in product.product_template_attribute_value_ids
                    if value.attribute_line_id in lines
                ]
            else:
                option_values = [{"optionName": "Title", "name": DEFAULT_TITLE}]
            data = {
                "optionValues": option_values,
                "sku": product.default_code or "",
                "price": f"{self._variant_price(product):.2f}",
                "taxable": bool(product.taxes_id),
                "inventoryItem": {
                    "tracked": bool(product.is_storable),
                    "cost": f"{product.standard_price:.2f}",
                    "requiresShipping": product.type != "service",
                },
            }
            if product.weight:
                data["inventoryItem"]["measurement"] = {
                    "weight": {"value": product.weight, "unit": self._weight_unit()},
                }
            if product.barcode:
                data["barcode"] = product.barcode
            if instance.compare_at_pricelist_id:
                data["compareAtPrice"] = f"{instance.compare_at_pricelist_id._get_product_price(product, 1.0):.2f}"
            if mapping.shopify_gid:
                data["id"] = mapping.shopify_gid
            metafields = Mapping._build_export(instance, "variant", product)
            if metafields:
                data["metafields"] = metafields
            variants.append(data)
        collections = self._collections_for_export()
        result = {
            "title": self.name or template.name,
            "descriptionHtml": self.description_html or "",
            "vendor": self.vendor or instance.company_id.name,
            "productType": self.product_type or template.categ_id.name or "",
            "status": self.status or ("DRAFT" if instance.export_as_draft else "ACTIVE"),
            "tags": [tag.strip() for tag in (self.tags or "").split(",") if tag.strip()],
            "productOptions": options,
            "variants": variants,
        }
        if collections:
            result["collections"] = collections
        if self.seo_title or self.seo_description:
            result["seo"] = {"title": self.seo_title or "", "description": self.seo_description or ""}
        metafields = Mapping._build_export(instance, "product", template)
        if metafields:
            result["metafields"] = metafields
        image = self._signed_image_url(template)
        if image:
            result["files"] = [{"originalSource": image, "contentType": "IMAGE", "alt": template.name}]
        return result

    def _weight_unit(self):
        name = (self.env["product.template"]._get_weight_uom_id_from_ir_config_parameter().name or "kg").lower()
        return {"kg": "KILOGRAMS", "g": "GRAMS", "lb": "POUNDS", "lbs": "POUNDS", "oz": "OUNCES"}.get(name, "KILOGRAMS")

    def _signed_image_url(self, template):
        """Shopify pulls images by URL, so expose only this one, under an HMAC signature."""
        self.ensure_one()
        if not template.image_1920:
            return False
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        if not base.startswith("https://"):
            return False
        return f"{base}/zivoit/shopify/image/{template.id}/{self._image_signature(template.id)}"

    @api.model
    def _image_signature(self, template_id):
        secret = self.env["ir.config_parameter"].sudo().get_param("database.secret")
        return hmac.new(secret.encode(), f"zivo-shopify-image:{template_id}".encode(), hashlib.sha256).hexdigest()

    def _export_to_shopify(self):
        self.ensure_one()
        if self.excluded:
            return False
        self = self.with_context(lang=self.instance_id._source_lang())
        instance = self.instance_id
        client = instance._client()
        identifier = {"id": self.shopify_gid} if self.shopify_gid else None
        payload = client.mutate(
            Q.PRODUCT_SET, {"input": self._build_product_set_input(), "identifier": identifier}, "productSet",
        )
        node = payload["product"]
        self._apply_export_result(node)
        if self.status == "ACTIVE":
            self._publish(client, True)
        self._queue_translation_export()
        return node["id"]

    def _apply_export_result(self, node):
        self.ensure_one()
        Variant = self.env["zivo.shopify.variant"]
        self.write({
            "shopify_gid": node["id"],
            "handle": node.get("handle"),
            "status": node.get("status"),
            "option_data": node.get("options") or [],
            "last_export_at": fields.Datetime.now(),
        })
        template = self.product_tmpl_id
        variant_nodes = list((node.get("variants") or {}).get("nodes") or [])
        page = (node.get("variants") or {}).get("pageInfo") or {}
        if page.get("hasNextPage"):
            variant_nodes += list(self.instance_id._client().iter_connection(
                Q.PRODUCT_VARIANTS_PAGE, {"id": node["id"], "cursor": page.get("endCursor")}, ["product", "variants"]))
        for variant_node in variant_nodes:
            product = self._product_for_options(template, variant_node.get("selectedOptions") or [])
            existing = Variant.search([("instance_id", "=", self.instance_id.id), ("shopify_gid", "=", variant_node["id"])], limit=1)
            vals = Variant._vals_from_node(variant_node)
            vals.update(product_id=product.id, stock_dirty=True)
            if existing:
                existing.write(vals)
            else:
                vals.update(shopify_product_id=self.id, shopify_gid=variant_node["id"])
                Variant.create(vals)

    @api.model
    def _product_for_options(self, template, selected):
        wanted = {(o["name"].lower(), o["value"].lower()) for o in selected if o.get("value") != DEFAULT_TITLE}
        for product in template.product_variant_ids:
            values = {
                (v.attribute_id.name.lower(), v.name.lower())
                for v in product.product_template_attribute_value_ids
                if v.attribute_id.create_variant != "no_variant"
            }
            if values == wanted:
                return product
        return template.product_variant_ids[:1] if not wanted else self.env["product.product"]

    def _publish(self, client, publish):
        self.ensure_one()
        instance = self.instance_id
        configured = [gid.strip() for gid in (instance.publication_gids or "").split(",") if gid.strip()]
        publications = configured or [p["id"] for p in instance.publication_data or []]
        if not publications or not self.shopify_gid:
            return
        client.mutate(
            Q.PUBLISH if publish else Q.UNPUBLISH,
            {"id": self.shopify_gid, "input": [{"publicationId": gid} for gid in publications]},
            "publishablePublish" if publish else "publishableUnpublish",
        )

    def _set_remote_status(self, status):
        client = self.instance_id._client()
        for product in self.filtered("shopify_gid"):
            client.mutate(Q.PRODUCT_STATUS, {"product": {"id": product.shopify_gid, "status": status}}, "productUpdate")
            product.status = status
            product._publish(client, status == "ACTIVE")

    def _export_prices(self):
        client = self.instance_id._client()
        for product in self.filtered("shopify_gid"):
            variants = []
            for variant in product.variant_ids.filtered("product_id"):
                vals = {"id": variant.shopify_gid, "price": f"{product._variant_price(variant.product_id):.2f}"}
                if product.instance_id.compare_at_pricelist_id:
                    vals["compareAtPrice"] = (
                        f"{product.instance_id.compare_at_pricelist_id._get_product_price(variant.product_id, 1.0):.2f}")
                variants.append(vals)
            if variants:
                client.mutate(Q.VARIANTS_BULK_UPDATE, {"productId": product.shopify_gid, "variants": variants},
                              "productVariantsBulkUpdate")
                for variant, vals in zip(product.variant_ids.filtered("product_id"), variants):
                    variant.price = float(vals["price"])

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_export(self):
        for product in self:
            product.instance_id._enqueue("export_product", f"Export {product.name}", {"product": product.id},
                                         dedupe_key=f"export_product:{product.id}", source="odoo")
        return True

    def action_export_prices(self):
        for product in self:
            product.instance_id._enqueue("export_prices", f"Prices {product.name}", {"product": product.id},
                                         dedupe_key=f"export_prices:{product.id}", source="odoo")
        return True

    def action_publish(self):
        for instance in self.instance_id:
            self.filtered(lambda p: p.instance_id == instance)._set_remote_status("ACTIVE")
        return True

    def action_unpublish(self):
        for instance in self.instance_id:
            self.filtered(lambda p: p.instance_id == instance)._set_remote_status("DRAFT")
        return True

    def action_refresh(self):
        for product in self.filtered("shopify_gid"):
            product.instance_id._enqueue("import_product", f"Refresh {product.name}", {"gid": product.shopify_gid},
                                         dedupe_key=f"product:{product.shopify_gid}")
        return True


class ZivoShopifyVariant(models.Model):
    _name = "zivo.shopify.variant"
    _description = "Shopify Variant"
    _order = "shopify_product_id desc, position, id"

    name = fields.Char(compute="_compute_name", store=True)
    shopify_product_id = fields.Many2one("zivo.shopify.product", required=True, ondelete="cascade", index=True)
    instance_id = fields.Many2one(related="shopify_product_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="shopify_product_id.company_id", store=True, index=True)
    product_id = fields.Many2one("product.product", string="Odoo Variant", index=True, ondelete="set null")
    shopify_gid = fields.Char(string="Shopify ID", index=True, required=True, copy=False)
    inventory_item_gid = fields.Char(index=True)
    title = fields.Char()
    sku = fields.Char(index=True)
    barcode = fields.Char()
    price = fields.Float(digits="Product Price")
    compare_at_price = fields.Float(digits="Product Price")
    unit_cost = fields.Float(digits="Product Price")
    position = fields.Integer()
    tracked = fields.Boolean(default=True)
    options = fields.Char()
    stock_dirty = fields.Boolean(index=True, default=True)
    last_stock_export_at = fields.Datetime(readonly=True)
    stock_policy = fields.Selection([
        ("default", "Store Rule"),
        ("fixed", "Fixed Quantity"),
        ("percent", "Percentage"),
        ("exclude", "Do Not Sync"),
    ], default="default", required=True)
    stock_policy_value = fields.Float(string="Policy Value")
    level_ids = fields.One2many("zivo.shopify.variant.level", "variant_id", string="Stock by Location")

    _gid_unique = models.Constraint("UNIQUE(shopify_product_id, shopify_gid)", "Variant already linked.")

    @api.depends("shopify_product_id.name", "title")
    def _compute_name(self):
        for variant in self:
            suffix = f" - {variant.title}" if variant.title and variant.title != DEFAULT_TITLE else ""
            variant.name = f"{variant.shopify_product_id.name or ''}{suffix}"

    @api.model
    def _vals_from_node(self, node):
        item = node.get("inventoryItem") or {}
        return {
            "title": node.get("title"),
            "sku": node.get("sku"),
            "barcode": next(iter(b.get("value") for b in (node.get("barcodes") or {}).get("nodes") or []), False),
            "price": float(node.get("price") or 0),
            "compare_at_price": float(node.get("compareAtPrice") or 0),
            "unit_cost": float((item.get("unitCost") or {}).get("amount") or 0),
            "position": node.get("position") or 0,
            "inventory_item_gid": item.get("id"),
            "tracked": item.get("tracked", True),
            "options": ", ".join(f"{o['name']}: {o['value']}" for o in node.get("selectedOptions") or []),
        }

    def _find_odoo_product(self):
        self.ensure_one()
        Product = self.env["product.product"].with_context(active_test=False)
        company = [("company_id", "in", [self.company_id.id, False])]
        if self.sku:
            found = Product.search([("default_code", "=", self.sku)] + company, limit=1)
            if found:
                return found
        if self.barcode:
            found = Product.search([("barcode", "=", self.barcode)] + company, limit=1)
            if found:
                return found
        return Product.browse()

    def _match_in_template(self, template):
        self.ensure_one()
        selected = []
        for part in (self.options or "").split(", "):
            if ": " in part:
                name, value = part.split(": ", 1)
                selected.append({"name": name, "value": value})
        return self.env["zivo.shopify.product"]._product_for_options(template, selected)

    def action_mark_stock_dirty(self):
        self.write({"stock_dirty": True})
        return True


class ProductTemplate(models.Model):
    _inherit = "product.template"

    zivo_shopify_product_ids = fields.One2many("zivo.shopify.product", "product_tmpl_id", string="Shopify Listings")
    zivo_shopify_count = fields.Integer(compute="_compute_zivo_shopify_count")

    @api.depends("zivo_shopify_product_ids")
    def _compute_zivo_shopify_count(self):
        for template in self:
            template.zivo_shopify_count = len(template.zivo_shopify_product_ids)

    def action_zivo_open_listings(self):
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_shopify_connector.action_products")
        action["domain"] = [("product_tmpl_id", "in", self.ids)]
        return action


class ZivoShopifyInstanceProducts(models.Model):
    _inherit = "zivo.shopify.instance"

    def action_import_products(self):
        self.ensure_one()
        self._enqueue("import_products", "Import all products", {}, dedupe_key="import_products")
        return self._notify(self.env._("Product import queued."), "success")

    def _job_import_products(self, job):
        payload = job.payload or {}
        client = self._client()
        data = client.execute(Q.PRODUCTS, {"query": payload.get("query"), "cursor": payload.get("cursor")}, expected_cost=150)["products"]
        for node in data["nodes"]:
            # One job per product keeps each read under Shopify's query cost limit.
            self._enqueue("import_product", f"Import {node['title']}", {"gid": node["id"], "force": True},
                          dedupe_key=f"product:{node['id']}", source=job.source, external_ref=node["id"], priority=8)
        info = data["pageInfo"]
        if info["hasNextPage"]:
            self._enqueue("import_products", f"Import products after {info['endCursor'][:12]}",
                          {**payload, "cursor": info["endCursor"]}, source=job.source)
        elif payload.get("stamp") or not payload.get("query"):
            self.last_product_sync = fields.Datetime.now()
        return f"{len(data['nodes'])} products queued"

    def _job_import_product(self, job):
        gid = job.payload["gid"]
        Product = self.env["zivo.shopify.product"]
        record = Product.search([("instance_id", "=", self.id), ("shopify_gid", "=", gid)], limit=1)
        # Our own export echoes back as products/update: nothing new to read.
        recent = record.last_export_at and record.last_export_at > fields.Datetime.now() - timedelta(seconds=90)
        if recent and not job.payload.get("force"):
            return "Skipped: echo of our own export"
        node = self._client().execute(Q.PRODUCT, {"id": gid}, expected_cost=500).get("product")
        if not node:
            return "Product no longer exists on Shopify"
        Product._upsert_from_node(self, node)
        return node["title"]

    def _job_export_product(self, job):
        product = self.env["zivo.shopify.product"].browse(job.payload["product"]).exists()
        if not product:
            return "Product removed"
        return product._export_to_shopify()

    def _job_export_prices(self, job):
        product = self.env["zivo.shopify.product"].browse(job.payload["product"]).exists()
        product._export_prices()
        return product.name

    def _job_product_deleted(self, job):
        product = self.env["zivo.shopify.product"].search(
            [("instance_id", "=", self.id), ("shopify_gid", "=", job.payload["gid"])], limit=1)
        product.write({"status": "ARCHIVED", "shopify_gid": False})
        product.variant_ids.unlink()
        return product.name or "Unknown product"
