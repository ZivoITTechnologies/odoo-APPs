from odoo import fields, models
from odoo.exceptions import UserError

from ..lib import queries as Q
from ..lib.client import ShopifyError


class ZivoShopifySetupWizard(models.TransientModel):
    _name = "zivo.shopify.setup.wizard"
    _description = "Connect a Shopify Store"

    step = fields.Selection([("credentials", "Connect"), ("automation", "Automate"), ("done", "Finished")], default="credentials", required=True)
    instance_id = fields.Many2one("zivo.shopify.instance")
    name = fields.Char(string="Store Name")
    shop_domain = fields.Char(string="Shop Domain")
    auth_mode = fields.Selection([
        ("client_credentials", "Dev Dashboard App (Client ID + Secret)"),
        ("admin_token", "Existing Custom App (Admin API Token)"),
    ], default="client_credentials", required=True)
    client_id = fields.Char(string="Client ID")
    client_secret = fields.Char(string="Client Secret")
    access_token = fields.Char(string="Admin API Token")
    webhook_secret = fields.Char(string="Webhook Signing Secret")
    register_webhooks = fields.Boolean(default=True)
    import_locations_done = fields.Boolean()
    setup_accounting = fields.Boolean(string="Create Shopify Payments Journal and Accounts", default=True)
    import_products = fields.Boolean(default=True)
    import_customers = fields.Boolean()
    import_markets = fields.Boolean()
    summary = fields.Text(readonly=True)

    def _reopen(self):
        return {"name": self.env._("Connect a Shopify Store"), "type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form", "target": "new"}

    def action_connect(self):
        self.ensure_one()
        if not self.name or not self.shop_domain:
            raise UserError(self.env._("Enter a store name and its myshopify.com domain."))
        vals = {
            "name": self.name, "shop_domain": self.shop_domain, "auth_mode": self.auth_mode,
            "client_id": self.client_id, "client_secret": self.client_secret,
            "access_token": self.access_token, "webhook_secret": self.webhook_secret,
        }
        if self.instance_id:
            self.instance_id.write(vals)
        else:
            self.instance_id = self.env["zivo.shopify.instance"].create(vals)
        self.instance_id.action_test_connection()
        self.step = "automation"
        return self._reopen()

    def action_finish(self):
        self.ensure_one()
        instance = self.instance_id
        done = []
        if self.setup_accounting:
            instance.action_setup_accounting()
            done.append(self.env._("Shopify Payments journal and accounts created"))
        if self.register_webhooks:
            instance.action_register_webhooks()
            done.append(self.env._("Webhooks registered"))
        if self.import_products:
            instance.action_import_products()
            done.append(self.env._("Product import queued"))
        if self.import_customers:
            instance.action_import_customers()
            done.append(self.env._("Customer import queued"))
        if self.import_markets:
            instance.action_import_markets()
            done.append(self.env._("Market import queued"))
        self.write({"step": "done", "summary": "\n".join(done)})
        return self._reopen()

    def action_open_store(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "zivo.shopify.instance", "res_id": self.instance_id.id, "view_mode": "form", "target": "current"}


class ZivoShopifyExportWizard(models.TransientModel):
    _name = "zivo.shopify.export.wizard"
    _description = "Export Products to Shopify"

    instance_id = fields.Many2one("zivo.shopify.instance", required=True, domain=[("state", "=", "connected")])
    product_tmpl_ids = fields.Many2many("product.template", string="Products", default=lambda self: self.env.context.get("active_ids"))
    status = fields.Selection([("DRAFT", "Draft"), ("ACTIVE", "Active")], default="DRAFT", required=True, string="Status on Shopify")

    def action_export(self):
        self.ensure_one()
        Product = self.env["zivo.shopify.product"]
        for template in self.product_tmpl_ids:
            record = Product._ensure_for_template(self.instance_id, template)
            if not record.shopify_gid:
                record.status = self.status
            record.action_export()
        return {"type": "ir.actions.act_window_close"}


class ZivoShopifyCancelWizard(models.TransientModel):
    _name = "zivo.shopify.cancel.wizard"
    _description = "Cancel Order on Shopify"

    order_id = fields.Many2one("sale.order", required=True)
    reason = fields.Selection([
        ("CUSTOMER", "Customer Changed Their Mind"), ("FRAUD", "Fraud"), ("INVENTORY", "Out of Stock"),
        ("DECLINED", "Payment Declined"), ("STAFF", "Staff Error"), ("OTHER", "Other"),
    ], default="OTHER", required=True)
    restock = fields.Boolean(default=True)
    refund = fields.Boolean(string="Refund to Original Payment", default=True)
    notify_customer = fields.Boolean(default=True)
    staff_note = fields.Char()

    def action_confirm(self):
        self.ensure_one()
        order = self.order_id
        order.check_access("write")
        client = order.zivo_instance_id.sudo()._client()
        data = client.execute(Q.ORDER_CANCEL, {
            "orderId": order.zivo_shopify_gid,
            "reason": self.reason,
            "restock": self.restock,
            "notifyCustomer": self.notify_customer,
            "staffNote": self.staff_note or None,
            "refundMethod": {"originalPaymentMethodsRefund": self.refund},
        })
        errors = data["orderCancel"].get("orderCancelUserErrors") or []
        if errors:
            raise UserError("; ".join(error["message"] for error in errors))
        order.message_post(body=self.env._("Cancellation sent to Shopify."))
        if order.state != "cancel" and not order.picking_ids.filtered(lambda p: p.state == "done") and not order.invoice_ids.filtered(lambda m: m.state == "posted"):
            order.with_context(disable_cancel_warning=True)._action_cancel()
        order.zivo_cancelled_on_shopify = True
        return {"type": "ir.actions.act_window_close"}


class ZivoShopifyOrderExportWizard(models.TransientModel):
    _name = "zivo.shopify.order.export.wizard"
    _description = "Create Order on Shopify"

    order_id = fields.Many2one("sale.order", required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", required=True, domain=[("state", "=", "connected")])

    def action_export(self):
        self.ensure_one()
        self.order_id.check_access("write")
        try:
            self.order_id._zivo_export_to_shopify(self.instance_id)
        except ShopifyError as error:
            raise UserError(str(error)) from error
        return {"type": "ir.actions.act_window_close"}
