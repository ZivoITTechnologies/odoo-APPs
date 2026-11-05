from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    epc_on_time_rate = fields.Float(string="On-time Delivery (%)", readonly=True, digits=(5, 1), copy=False)
    epc_avg_delay_days = fields.Float(string="Average Delay (days)", readonly=True, digits=(6, 1), copy=False)
    epc_delivery_samples = fields.Integer(string="Deliveries Analysed", readonly=True, copy=False)
    epc_supplier_risk_score = fields.Float(
        string="Supplier Risk Score", readonly=True, digits=(5, 1), copy=False,
        help="0 = very reliable, 100 = very risky. Learned from this vendor's delivery history.")
    epc_supplier_risk_date = fields.Datetime(string="Risk Scored On", readonly=True, copy=False)
