from unittest.mock import patch

from odoo import fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .shopify_mock import ShopifyMock


class ZivoShopifyCommon(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, zivo_shopify_inline=True, tracking_disable=True))
        cls.env.user.sudo().group_ids |= (
            cls.env.ref("zivoit_shopify_connector.group_manager")
            | cls.env.ref("sales_team.group_sale_manager")
            | cls.env.ref("stock.group_stock_manager")
            | cls.env.ref("account.group_account_manager")
            | cls.env.ref("product.group_product_manager")
            | cls.env.ref("uom.group_uom")
        )
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.instance = cls.env["zivo.shopify.instance"].create({
            "name": "Acme", "shop_domain": "acme.myshopify.com", "client_id": "cid", "client_secret": "secret",
            "warehouse_id": cls.warehouse.id, "auto_create_products": True,
        })

    def setUp(self):
        super().setUp()
        self.shop = ShopifyMock()
        for target in ("requests.post", "requests.get"):
            patcher = patch(target, getattr(self.shop, target.split(".")[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch("time.sleep", lambda _s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.instance.action_test_connection()
        self.location = self.instance.location_ids[:1]

    def run_queue(self):
        Job = self.env["zivo.shopify.job"]
        # Page jobs queue record jobs, as the real scheduler picks up on its next pass.
        for _pass in range(5):
            Job._cron_process()
            self.env.invalidate_all()
            if not Job.search_count([("state", "=", "pending"), ("next_attempt_at", "<=", fields.Datetime.now())]):
                break

    def jobs(self, state=None):
        domain = [("instance_id", "=", self.instance.id)]
        if state:
            domain.append(("state", "=", state))
        return self.env["zivo.shopify.job"].search(domain)

    def stock_product(self, sku="SKU-1", name="Blue Tee", qty=50.0, gid="11", price=100.0):
        product = self.env["product.product"].create({
            "name": name, "default_code": sku, "is_storable": True, "type": "consu", "list_price": price, "invoice_policy": "order",
        })
        if qty:
            self.env["stock.quant"].with_context(inventory_mode=True).create({
                "product_id": product.id, "location_id": self.warehouse.lot_stock_id.id, "inventory_quantity": qty,
            }).action_apply_inventory()
        if gid:
            mapping = self.env["zivo.shopify.product"].create({
                "instance_id": self.instance.id, "name": name, "shopify_gid": f"gid://shopify/Product/{gid}", "product_tmpl_id": product.product_tmpl_id.id, "status": "ACTIVE",
            })
            self.env["zivo.shopify.variant"].create({
                "shopify_product_id": mapping.id, "shopify_gid": f"gid://shopify/ProductVariant/{gid}", "product_id": product.id,
                "sku": sku, "inventory_item_gid": f"gid://shopify/InventoryItem/{gid}", "stock_dirty": False,
            })
        return product

    def import_order(self, node):
        self.shop.orders[node["id"]] = node
        order, status = self.env["sale.order"]._zivo_import_order(self.instance, self.instance._fetch_order(node["id"]))
        return order, status

    def enable_full_workflow(self):
        self.instance.action_setup_accounting()
        self.instance.workflow_ids[:1].write({
            "create_invoice": True, "post_invoice": True, "register_payment": True,
            "payment_journal_id": self.instance.clearing_journal_id.id,
        })
