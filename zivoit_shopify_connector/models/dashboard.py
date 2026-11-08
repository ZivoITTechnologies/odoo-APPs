import json
from datetime import timedelta

from odoo import api, fields, models

from ..lib import ai as ai_lib


# Old names that PostgreSQL may not know, such as on Ubuntu 24.04 without tzdata-legacy.
LEGACY_TIMEZONES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Dacca": "Asia/Dhaka",
}


class ZivoShopifyDashboard(models.AbstractModel):
    _name = "zivo.shopify.dashboard"
    _description = "Shopify Dashboard"

    @api.model
    def _instances(self, instance_id=None):
        Instance = self.env["zivo.shopify.instance"]
        if instance_id:
            return Instance.search([("id", "=", int(instance_id))])
        return Instance.search([])

    @api.model
    def get_data(self, instance_id=None):
        timezone = self.env.context.get("tz") or self.env.user.tz
        self = self.with_context(tz=LEGACY_TIMEZONES.get(timezone, timezone))
        instances = self._instances(instance_id)
        ids = instances.ids
        now = fields.Datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        Order = self.env["sale.order"]
        Job = self.env["zivo.shopify.job"]
        order_domain = [("zivo_instance_id", "in", ids), ("state", "!=", "cancel")]

        start = (now - timedelta(days=13)).date()
        rows = Order._read_group(order_domain + [("date_order", ">=", fields.Datetime.to_datetime(start))],
                                 ["date_order:day"], ["amount_total:sum", "__count"])
        by_day = {fields.Date.to_date(day): (total, count) for day, total, count in rows}
        series = []
        for offset in range(14):
            day = start + timedelta(days=offset)
            total, count = by_day.get(day, (0.0, 0))
            series.append({"label": day.strftime("%d %b"), "revenue": round(total, 2), "orders": count})

        states = dict(Job._read_group([("instance_id", "in", ids), ("state", "in", ("pending", "running", "failed"))], ["state"], ["__count"]))
        done_24 = Job.search_count([("instance_id", "in", ids), ("state", "=", "done"), ("write_date", ">=", now - timedelta(hours=24))])
        failed_24 = Job.search_count([("instance_id", "in", ids), ("state", "=", "failed"), ("write_date", ">=", now - timedelta(hours=24))])
        total_24 = done_24 + failed_24
        today_rows = Order._read_group(order_domain + [("date_order", ">=", today)], [], ["amount_total:sum", "__count"])
        currency = (instances[:1].shop_currency_id or self.env.company.currency_id)

        webhooks = self.env["zivo.shopify.webhook"].search([("instance_id", "in", ids), ("last_received_at", "!=", False)], order="last_received_at desc", limit=1)
        feed = [{
            "id": job.id,
            "name": job.name,
            "state": job.state,
            "source": job.source,
            "store": job.instance_id.name,
            "when": fields.Datetime.to_string(job.write_date),
            "error": job.ai_root_cause or "",
        } for job in Job.search([("instance_id", "in", ids)], limit=12, order="write_date desc, id desc")]

        return {
            "instances": [{
                "id": inst.id, "name": inst.name, "state": inst.state, "domain": inst.shop_domain,
                "api_available": inst.api_available, "api_maximum": inst.api_maximum,
            } for inst in self._instances()],
            "currency": currency.name,
            "kpis": {
                "orders_today": today_rows[0][1],
                "revenue_today": round(today_rows[0][0] or 0.0, 2),
                "pending": states.get("pending", 0) + states.get("running", 0),
                "failed": states.get("failed", 0),
                "success_rate": round(done_24 / total_24 * 100, 1) if total_24 else 100.0,
                "products": self.env["zivo.shopify.product"].search_count([("instance_id", "in", ids)]),
                "unmatched": self.env["zivo.shopify.match"].search_count([("instance_id", "in", ids), ("state", "=", "suggested")]),
                "risk_holds": Order.search_count([("zivo_instance_id", "in", ids), ("zivo_risk_hold", "=", True)]),
                "drift": self.env["zivo.shopify.drift"].search_count([("instance_id", "in", ids), ("state", "=", "open")]),
                "to_ship": self.env["stock.picking"].search_count([("sale_id.zivo_instance_id", "in", ids), ("state", "=", "assigned"), ("picking_type_code", "=", "outgoing")]),
                "payouts_todo": self.env["zivo.shopify.payout"].search_count([("instance_id", "in", ids), ("state", "=", "draft")]),
            },
            "series": series,
            "feed": feed,
            "insights": self._insights(instances),
            "last_webhook": fields.Datetime.to_string(webhooks.last_received_at) if webhooks else False,
            "llm_ready": any(inst._ai_available() for inst in instances),
            "server_time": fields.Datetime.to_string(now),
        }

    @api.model
    def _insights(self, instances):
        ids = instances.ids
        insights = []
        Forecast = self.env["zivo.shopify.forecast"]
        critical = Forecast.search([("instance_id", "in", ids), ("risk", "=", "critical")], limit=3, order="days_of_cover")
        for row in critical:
            insights.append({
                "kind": "danger", "title": f"{row.product_id.display_name} runs out in {row.days_of_cover:.0f} days",
                "detail": f"Selling {row.daily_velocity:.1f} a day. Suggested reorder: {row.suggested_reorder}.",
            })
        holds = self.env["sale.order"].search([("zivo_instance_id", "in", ids), ("zivo_risk_hold", "=", True)], limit=2, order="zivo_risk_score desc")
        for order in holds:
            insights.append({
                "kind": "warning", "title": f"{order.name} is on risk hold ({order.zivo_risk_score}/100)",
                "detail": (order.zivo_risk_reasons or "").split("\n")[0],
            })
        failed = self.env["zivo.shopify.job"]._read_group(
            [("instance_id", "in", ids), ("state", "=", "failed")], ["ai_root_cause"], ["__count"], order="__count desc", limit=2)
        for cause, count in failed:
            insights.append({"kind": "danger", "title": f"{count} failed job(s): {cause or 'Unclassified'}", "detail": "Open Failed Jobs for the recommended fix."})
        anomalies = self.env["zivo.shopify.payout"].search([("instance_id", "in", ids), ("anomaly_count", ">", 0)], limit=1, order="id desc")
        for payout in anomalies:
            insights.append({"kind": "warning", "title": f"{payout.name}: {payout.anomaly_count} anomaly", "detail": (payout.anomaly_note or "").split("\n")[0]})
        rising = Forecast.search([("instance_id", "in", ids), ("trend_percent", ">=", 40), ("risk", "in", ("ok", "warning"))], limit=2, order="trend_percent desc")
        for row in rising:
            insights.append({"kind": "success", "title": f"{row.product_id.display_name} demand up {row.trend_percent:.0f}%", "detail": "Compared with the previous 14 days."})
        if not insights:
            insights.append({"kind": "success", "title": "All clear", "detail": "No stock, risk or sync issue needs attention."})
        return insights[:7]

    @api.model
    def ask_copilot(self, question, instance_id=None):
        """Answer a question from aggregates only: no customer data reaches the LLM."""
        instances = self._instances(instance_id)
        data = self.get_data(instance_id)
        context = {
            "currency": data["currency"],
            "kpis": data["kpis"],
            "last_14_days": data["series"],
            "insights": data["insights"],
        }
        store = instances.filtered(lambda inst: inst._ai_available())[:1]
        if store:
            try:
                answer = store._ai_complete(
                    "copilot",
                    "You are the operations copilot of a Shopify and Odoo connector. Answer briefly and only "
                    "from the JSON data supplied. If the data does not contain the answer, say so.",
                    json.dumps({"question": (question or "")[:500], "data": context}), max_tokens=350,
                )
                return {"answer": answer, "engine": "llm"}
            except ai_lib.AIError:
                pass
        return {"answer": self._local_answer(question, data), "engine": "local"}

    @api.model
    def _local_answer(self, question, data):
        text = (question or "").lower()
        kpi = data["kpis"]
        rules = [
            (("fail", "error", "problem"), f"{kpi['failed']} jobs have failed. Last 24h success rate: {kpi['success_rate']}%."),
            (("revenue", "sales", "sold", "today"), f"Today: {kpi['orders_today']} orders worth {kpi['revenue_today']} {data['currency']}."),
            (("stock", "inventory", "drift"), f"{kpi['drift']} stock differences are open."),
            (("risk", "fraud", "hold"), f"{kpi['risk_holds']} orders are on risk hold."),
            (("match", "unmapped", "link"), f"{kpi['unmatched']} product matches are waiting for review."),
            (("ship", "deliver", "fulfil"), f"{kpi['to_ship']} deliveries are ready to ship."),
            (("payout",), f"{kpi['payouts_todo']} payouts are waiting to be processed."),
            (("queue", "pending"), f"{kpi['pending']} jobs are in the queue."),
        ]
        for keys, answer in rules:
            if any(key in text for key in keys):
                return answer
        return "I can report on failures, revenue, stock drift, risk holds, matches, shipping, payouts and the queue. Add an LLM key for free-form answers."
