from odoo.tests import tagged
from odoo.fields import Command
from odoo.tools import mute_logger

from .common import ZivoShopifyCommon
from .shopify_mock import make_product, make_variant


@tagged("post_install", "-at_install")
class TestProducts(ZivoShopifyCommon):

    def variant_product(self):
        options = [{"name": "Size", "position": 1, "values": ["S", "M"]}, {"name": "Color", "position": 2, "values": ["Red"]}]
        return make_product("100", "Runner Shoe", [
            make_variant("201", "RUN-S", "50.00", [{"name": "Size", "value": "S"}, {"name": "Color", "value": "Red"}], "S / Red", barcode="111"),
            make_variant("202", "RUN-M", "55.00", [{"name": "Size", "value": "M"}, {"name": "Color", "value": "Red"}], "M / Red", position=2),
        ], options)

    def do_import(self):
        self.shop.products["gid://shopify/Product/100"] = self.variant_product()
        self.instance.action_import_products()
        self.run_queue()
        return self.env["zivo.shopify.product"].search([("instance_id", "=", self.instance.id), ("shopify_gid", "=", "gid://shopify/Product/100")])

    def test_import_creates_template_with_variants(self):
        record = self.do_import()
        self.assertEqual(self.jobs("failed"), self.env["zivo.shopify.job"])
        template = record.product_tmpl_id
        self.assertEqual(template.name, "Runner Shoe")
        self.assertEqual(len(template.product_variant_ids), 2)
        self.assertTrue(template.is_storable)
        small = record.variant_ids.filtered(lambda v: v.sku == "RUN-S")
        self.assertEqual(small.product_id.default_code, "RUN-S")
        self.assertEqual(small.product_id.barcode, "111")
        self.assertEqual(small.product_id.standard_price, 10.0)
        self.assertIn("S", small.product_id.product_template_attribute_value_ids.mapped("name"))
        self.assertEqual(self.instance.last_product_sync.date(), record.last_import_at.date())

    def test_import_downloads_main_image(self):
        node = make_product("110", "With Image", [make_variant("111", "IMG-1")])
        node["media"] = {"nodes": [{"__typename": "MediaImage", "id": "gid://shopify/MediaImage/1", "alt": None, "image": {"url": "https://cdn.test/a.png", "altText": None}}], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        self.shop.products[node["id"]] = node
        self.instance.action_import_products()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        record = self.env["zivo.shopify.product"].search([("shopify_gid", "=", node["id"])])
        self.assertTrue(record.product_tmpl_id.image_1920)

    def test_reimport_does_not_duplicate(self):
        record = self.do_import()
        count = self.env["product.template"].search_count([("name", "=", "Runner Shoe")])
        self.instance.action_import_products()
        self.run_queue()
        self.assertEqual(self.env["product.template"].search_count([("name", "=", "Runner Shoe")]), count)
        self.assertEqual(len(record.variant_ids), 2)

    def test_existing_product_is_linked_by_sku(self):
        existing = self.env["product.product"].create({"name": "Old name", "default_code": "ONLY-1", "is_storable": True})
        self.shop.products["p"] = make_product("300", "New name", [make_variant("301", "ONLY-1")])
        self.instance.action_import_products()
        self.run_queue()
        mapping = self.env["zivo.shopify.variant"].search([("sku", "=", "ONLY-1"), ("instance_id", "=", self.instance.id)])
        self.assertEqual(mapping.product_id, existing)
        self.assertEqual(existing.name, "Old name", "not overwritten unless asked")

    def test_removed_variant_disappears(self):
        record = self.do_import()
        node = self.variant_product()
        node["variants"]["nodes"].pop()
        self.shop.products["gid://shopify/Product/100"] = node
        self.instance.action_import_products()
        self.run_queue()
        self.assertEqual(len(record.variant_ids), 1)

    def test_export_new_product_with_attributes(self):
        attribute = self.env["product.attribute"].create({"name": "Size", "create_variant": "always"})
        small = self.env["product.attribute.value"].create({"attribute_id": attribute.id, "name": "S"})
        large = self.env["product.attribute.value"].create({"attribute_id": attribute.id, "name": "L"})
        template = self.env["product.template"].create({
            "name": "Exported Tee", "is_storable": True, "list_price": 20,
            "attribute_line_ids": [Command.create({"attribute_id": attribute.id, "value_ids": [Command.set([small.id, large.id])]})],
        })
        wizard = self.env["zivo.shopify.export.wizard"].create({"instance_id": self.instance.id, "product_tmpl_ids": [Command.set(template.ids)]})
        wizard.action_export()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        sent = self.shop.ops("ZivoProductSet")[0][1]["input"]
        self.assertEqual(sent["status"], "DRAFT")
        self.assertEqual({v["optionValues"][0]["name"] for v in sent["variants"]}, {"S", "L"})
        record = self.env["zivo.shopify.product"].search([("product_tmpl_id", "=", template.id)])
        self.assertTrue(record.shopify_gid)
        self.assertEqual(record.variant_ids.product_id, template.product_variant_ids)
        self.assertTrue(all(v.inventory_item_gid for v in record.variant_ids))

    def test_export_simple_product_uses_default_option(self):
        template = self.env["product.template"].create({"name": "Plain", "is_storable": True, "default_code": "P1"})
        record = self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)
        record._export_to_shopify()
        sent = self.shop.ops("ZivoProductSet")[0][1]["input"]
        self.assertEqual(sent["productOptions"][0]["values"][0]["name"], "Default Title")
        self.assertEqual(record.variant_ids.product_id, template.product_variant_id)

    def test_active_product_is_published(self):
        template = self.env["product.template"].create({"name": "Live"})
        record = self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)
        record.status = "ACTIVE"
        record._export_to_shopify()
        self.assertTrue(self.shop.ops("ZivoPublish"))

    def test_export_echo_is_not_reimported(self):
        template = self.env["product.template"].create({"name": "Echo"})
        record = self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)
        record._export_to_shopify()
        job = self.instance._enqueue("import_product", "echo", {"gid": record.shopify_gid})
        self.run_queue()
        self.assertIn("echo", job.result)

    def test_excluded_product_is_left_alone(self):
        record = self.do_import()
        record.excluded = True
        record.name = "Keep me"
        self.instance.action_import_products()
        self.run_queue()
        self.assertEqual(record.name, "Keep me")

    def test_price_export(self):
        product = self.stock_product()
        record = self.env["zivo.shopify.variant"].search([("product_id", "=", product.id)]).shopify_product_id
        record.action_export_prices()
        self.run_queue()
        self.assertEqual(self.shop.ops("ZivoVariantPrices")[0][1]["variants"][0]["price"], "100.00")

    def test_publish_unpublish(self):
        product = self.stock_product()
        record = self.env["zivo.shopify.variant"].search([("product_id", "=", product.id)]).shopify_product_id
        self.shop.products[record.shopify_gid] = make_product("11", "X", [make_variant("11", "SKU-1")])
        record.action_unpublish()
        self.assertEqual(record.status, "DRAFT")
        self.assertTrue(self.shop.ops("ZivoUnpublish"))

    def test_metafield_import_and_export(self):
        field = self.env["ir.model.fields"].search([("model", "=", "product.template"), ("name", "=", "description_sale")])
        self.env["zivo.shopify.metafield.mapping"].create({"instance_id": self.instance.id, "owner_type": "product", "namespace": "custom", "key": "care", "field_id": field.id})
        self.shop.products["p"] = make_product("400", "Meta", [make_variant("401", "MET")], metafields=[{"namespace": "custom", "key": "care", "type": "single_line_text_field", "value": "Hand wash"}])
        self.instance.action_import_products()
        self.run_queue()
        record = self.env["zivo.shopify.product"].search([("shopify_gid", "=", "gid://shopify/Product/400")])
        self.assertEqual(record.product_tmpl_id.description_sale, "Hand wash")
        built = record._build_product_set_input()
        self.assertEqual(built["metafields"][0]["value"], "Hand wash")

    def test_product_deleted_webhook(self):
        record = self.do_import()
        self.env["zivo.shopify.webhook"]._dispatch(self.instance, "PRODUCTS_DELETE", {"id": 100})
        self.run_queue()
        self.assertEqual(record.status, "ARCHIVED")
        self.assertFalse(record.variant_ids)


@tagged("post_install", "-at_install")
class TestStock(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.product = self.stock_product(qty=40)
        self.variant = self.env["zivo.shopify.variant"].search([("product_id", "=", self.product.id)])
        self.key = (self.variant.inventory_item_gid, self.location.shopify_gid)

    def test_export_sends_absolute_quantity(self):
        self.instance._export_stock(self.variant)
        self.assertEqual(self.shop.levels[self.key], 40)
        sent = self.shop.ops("ZivoInventorySet")[0][1]
        self.assertEqual(sent["input"]["name"], "available")
        self.assertIn("changeFromQuantity", sent["input"]["quantities"][0])
        self.assertTrue(sent["key"], "idempotency key is required by the API")

    def test_unchanged_quantity_costs_no_call(self):
        self.instance._export_stock(self.variant)
        calls = len(self.shop.ops("ZivoInventorySet"))
        self.instance._export_stock(self.variant)
        self.assertEqual(len(self.shop.ops("ZivoInventorySet")), calls)

    def test_percent_and_fixed_rules(self):
        self.instance.stock_percent = 50
        self.instance._export_stock(self.variant)
        self.assertEqual(self.shop.levels[self.key], 20)
        self.variant.write({"stock_policy": "fixed", "stock_policy_value": 7})
        self.instance._export_stock(self.variant)
        self.assertEqual(self.shop.levels[self.key], 7)

    def test_excluded_variant(self):
        self.variant.stock_policy = "exclude"
        self.instance._export_stock(self.variant)
        self.assertNotIn(self.key, self.shop.levels)

    def test_sources(self):
        self.instance.stock_source = "on_hand"
        self.assertEqual(self.instance._variant_stock(self.variant, self.location), 40)
        self.instance.stock_source = "forecast"
        self.assertEqual(self.instance._variant_stock(self.variant, self.location), 40)

    def test_drift_cron_survives_an_unreachable_store(self):
        from ..lib.client import ShopifyTransientError
        from unittest.mock import patch
        with patch.object(type(self.instance), "_scan_drift", side_effect=ShopifyTransientError("down")), mute_logger("odoo.addons.zivoit_shopify_connector.models.inventory"):
            self.env["zivo.shopify.instance"]._cron_drift_guard()

    def test_kit_stock_comes_from_components(self):
        if "mrp.bom" not in self.env:
            self.skipTest("Manufacturing is not installed")
        component = self.stock_product(sku="COMP-1", name="Component", qty=9, gid=None)
        kit = self.stock_product(sku="KIT-1", name="Kit", qty=0, gid="66")
        self.env["mrp.bom"].create({
            "product_tmpl_id": kit.product_tmpl_id.id, "type": "phantom", "product_qty": 1,
            "bom_line_ids": [Command.create({"product_id": component.id, "product_qty": 3})],
        })
        variant = self.env["zivo.shopify.variant"].search([("product_id", "=", kit.id)])
        self.instance.stock_source = "on_hand"
        self.assertEqual(self.instance._variant_stock(variant, self.location), 3)

    def test_never_negative(self):
        self.variant.write({"stock_policy": "percent", "stock_policy_value": 100})
        self.env["stock.quant"].with_context(inventory_mode=True).search([("product_id", "=", self.product.id), ("location_id", "=", self.warehouse.lot_stock_id.id)]).write({"inventory_quantity": 0})
        self.env["stock.quant"].search([("product_id", "=", self.product.id), ("location_id", "=", self.warehouse.lot_stock_id.id)]).action_apply_inventory()
        self.assertEqual(self.instance._variant_stock(self.variant, self.location), 0)

    def test_multi_warehouse_location_sums(self):
        second = self.env["stock.warehouse"].create({"name": "Second", "code": "SEC", "company_id": self.env.company.id})
        self.env["stock.quant"].with_context(inventory_mode=True).create({"product_id": self.product.id, "location_id": second.lot_stock_id.id, "inventory_quantity": 5}).action_apply_inventory()
        self.location.warehouse_ids = [Command.link(second.id)]
        self.assertEqual(self.instance._variant_stock(self.variant, self.location), 45)

    def test_stock_move_flags_variant(self):
        self.assertFalse(self.variant.stock_dirty)
        self.env["stock.quant"].with_context(inventory_mode=True).search([("product_id", "=", self.product.id), ("location_id", "=", self.warehouse.lot_stock_id.id)]).write({"inventory_quantity": 12})
        self.env["stock.quant"].search([("product_id", "=", self.product.id), ("location_id", "=", self.warehouse.lot_stock_id.id)]).action_apply_inventory()
        self.assertTrue(self.variant.stock_dirty)
        self.env["zivo.shopify.instance"]._cron_export_stock()
        self.run_queue()
        self.assertEqual(self.shop.levels[self.key], 12)
        self.assertFalse(self.variant.stock_dirty)

    def test_item_not_stocked_is_activated(self):
        self.shop.not_stocked_once = True
        self.instance._export_stock(self.variant)
        self.assertTrue(self.shop.ops("ZivoInventoryActivate"))
        self.assertEqual(self.shop.levels[self.key], 40)

    def test_drift_detected_and_corrected(self):
        self.instance._export_stock(self.variant)
        self.shop.levels[self.key] = 33
        drift = self.instance._scan_drift()
        self.assertEqual((drift.odoo_qty, drift.shopify_qty, drift.difference), (40, 33, -7))
        drift.action_fix()
        self.assertEqual(self.shop.levels[self.key], 40)
        self.assertEqual(drift.state, "fixed")

    def test_no_drift_when_equal(self):
        self.instance._export_stock(self.variant)
        self.assertFalse(self.instance._scan_drift())

    def test_import_stock_adjusts_quant(self):
        self.location.import_warehouse_id = self.warehouse
        self.shop.levels[self.key] = 9
        self.instance.action_import_stock()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        self.assertEqual(self.product.with_context(warehouse_id=self.warehouse.id).qty_available, 9)

    def test_inventory_webhook_updates_level(self):
        self.env["zivo.shopify.webhook"]._dispatch(self.instance, "INVENTORY_LEVELS_UPDATE", {"inventory_item_id": 11, "location_id": 1, "available": 4})
        level = self.env["zivo.shopify.variant.level"].search([("variant_id", "=", self.variant.id)])
        self.assertEqual(level.shopify_available, 4)

    def test_smart_buffer_reduces_published_stock(self):
        self.env["zivo.shopify.forecast"].create({"variant_id": self.variant.id, "daily_velocity": 3.2, "risk": "ok"})
        self.instance.smart_buffer_enabled = True
        self.instance.smart_buffer_hours = 24
        self.assertEqual(self.instance._variant_stock(self.variant, self.location), 36)
