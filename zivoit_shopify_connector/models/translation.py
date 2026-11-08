import json
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..lib import ai as ai_lib, queries as Q
from ..lib.client import ShopifyError

_logger = logging.getLogger(__name__)

BASE_LANG = "en_US"
ID_BATCH = 50


class ZivoShopifyLanguage(models.Model):
    _name = "zivo.shopify.language"
    _description = "Shopify Store Language"
    _order = "primary desc, name"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    locale = fields.Char(required=True, readonly=True)
    primary = fields.Boolean(readonly=True)
    published = fields.Boolean(readonly=True)
    lang_id = fields.Many2one("res.lang", string="Odoo Language", ondelete="set null")
    sync = fields.Boolean(string="Translate")

    _locale_unique = models.Constraint("UNIQUE(instance_id, locale)", "Language already imported.")

    @api.model
    def _find_odoo_language(self, locale):
        """``pt-BR`` -> pt_BR, ``fr`` -> fr_FR. Inactive languages count: they are activated on use."""
        Lang = self.env["res.lang"].with_context(active_test=False)
        code = locale.replace("-", "_")
        short = code.split("_")[0]
        found = Lang.search([("code", "=", code)], limit=1) or Lang.search([("iso_code", "=", code)], limit=1)
        if found:
            return found
        candidates = Lang.search([("code", "=like", f"{short}\\_%")], order="active desc, id")
        return candidates.filtered(lambda lang: lang.code.lower() == f"{short}_{short}")[:1] or candidates[:1]


class ZivoShopifyInstanceTranslations(models.Model):
    _inherit = "zivo.shopify.instance"

    def _sync_languages(self, client=None):
        self.ensure_one()
        client = client or self._client()
        try:
            locales = client.execute(Q.SHOP_LOCALES, expected_cost=10)["shopLocales"]
        except ShopifyError as error:
            _logger.info("Shopify languages unavailable: %s", error)
            return
        Language = self.env["zivo.shopify.language"]
        for node in locales:
            row = self.language_ids.filtered(lambda l: l.locale == node["locale"])
            vals = {"name": node["name"], "primary": node["primary"], "published": node["published"]}
            if row:
                row.write(vals)
            else:
                lang = Language._find_odoo_language(node["locale"])
                Language.create({**vals, "instance_id": self.id, "locale": node["locale"], "lang_id": lang.id,
                                 "sync": bool(lang and node["published"] and not node["primary"])})

    def action_sync_languages(self):
        self.ensure_one()
        self._sync_languages()
        return self._notify(self.env._("Languages refreshed."), "success")

    def _activate(self, lang):
        if lang and not lang.active:
            lang.sudo().active = True

    def _source_lang(self):
        """Odoo language holding the store's main content."""
        self.ensure_one()
        primary = self.sudo().language_ids.filtered("primary")[:1].lang_id
        self._activate(primary)
        return primary.code or BASE_LANG

    def _translation_rows(self):
        self.ensure_one()
        rows = self.language_ids.filtered(lambda row: row.sync and row.lang_id and not row.primary)
        for row in rows:
            self._activate(row.lang_id)
        return rows

    def _fetch_translatable(self, client, gids, locale):
        result = {}
        for start in range(0, len(gids), ID_BATCH):
            nodes = client.iter_connection(
                Q.TRANSLATABLE_RESOURCES, {"ids": gids[start:start + ID_BATCH], "locale": locale},
                ["translatableResourcesByIds"], expected_cost=200,
            )
            for node in nodes:
                result[node["resourceId"]] = node
        return result

    def _register_translations(self, client, gid, content, wanted, locale):
        """Send only what differs. Digests prove which source text a translation belongs to."""
        translations = []
        for item in content.get("translatableContent") or []:
            value = wanted.get(item["key"])
            if value and item.get("digest") and value != item.get("value"):
                translations.append({"locale": locale, "key": item["key"], "value": value, "translatableContentDigest": item["digest"]})
        if translations:
            client.mutate(Q.TRANSLATIONS_REGISTER, {"resourceId": gid, "translations": translations}, "translationsRegister")
        return len(translations)

    def _job_import_translations(self, job):
        rows = self._translation_rows()
        if not rows:
            return "No language to translate"
        client = self._client()
        count = 0
        if job.payload.get("collections"):
            collections = self.collection_ids
            gids = collections.mapped("shopify_gid")
            for row in rows:
                resources = self._fetch_translatable(client, gids, row.locale)
                for collection in collections:
                    value = self._translated_value(resources.get(collection.shopify_gid), "title")
                    if value:
                        collection.with_context(lang=row.lang_id.code).name = value
                        count += 1
            return f"{count} collection titles"
        product = self.env["zivo.shopify.product"].browse(job.payload["product"]).exists()
        if not product.shopify_gid:
            return "Product is not on Shopify"
        for row in rows:
            resources = self._fetch_translatable(client, product._translation_gids(), row.locale)
            count += product._apply_translations(resources, row.lang_id.code)
        return f"{count} texts"

    @staticmethod
    def _translated_value(resource, key):
        for item in (resource or {}).get("translations") or []:
            if item["key"] == key and item.get("value") and not item.get("outdated"):
                return item["value"]
        return None

    def _job_export_translations(self, job):
        rows = self._translation_rows()
        if not rows:
            return "No language to translate"
        client = self._client()
        count = 0
        if job.payload.get("collections"):
            collections = self.collection_ids
            gids = collections.mapped("shopify_gid")
            source = self._source_lang()
            for row in rows:
                resources = self._fetch_translatable(client, gids, row.locale)
                for collection in collections:
                    value = collection.with_context(lang=row.lang_id.code).name
                    if value != collection.with_context(lang=source).name and collection.shopify_gid in resources:
                        count += self._register_translations(client, collection.shopify_gid, resources[collection.shopify_gid], {"title": value}, row.locale)
            return f"{count} collection titles"
        product = self.env["zivo.shopify.product"].browse(job.payload["product"]).exists()
        if not product.shopify_gid:
            return "Product is not on Shopify"
        for row in rows:
            resources = self._fetch_translatable(client, product._translation_gids(), row.locale)
            specs = product._translation_values(row.lang_id.code)
            for gid, wanted in specs.items():
                if gid in resources:
                    count += self._register_translations(client, gid, resources[gid], wanted, row.locale)
        return f"{count} texts sent"

    def _queue_collection_translations(self):
        self.ensure_one()
        if self.sync_translations and self._translation_rows():
            self._enqueue("import_translations", "Import collection translations", {"collections": True},
                          dedupe_key="translations:collections", source="cron")

    def action_import_translations(self):
        self.ensure_one()
        self.env["zivo.shopify.product"].search([("instance_id", "=", self.id), ("shopify_gid", "!=", False)]).action_import_translations()
        self._enqueue("import_translations", "Import collection translations", {"collections": True}, dedupe_key="translations:collections")
        return self._notify(self.env._("Translation import queued."), "success")

    def action_export_translations(self):
        self.ensure_one()
        self.env["zivo.shopify.product"].search([("instance_id", "=", self.id), ("shopify_gid", "!=", False)]).action_export_translations()
        self._enqueue("export_translations", "Export collection translations", {"collections": True}, dedupe_key="export_translations:collections")
        return self._notify(self.env._("Translation export queued."), "success")


class ZivoShopifyProductTranslations(models.Model):
    _inherit = "zivo.shopify.product"

    def _queue_translation_import(self):
        self.ensure_one()
        store = self.instance_id
        if store.sync_translations and self.shopify_gid and store._translation_rows():
            store._enqueue("import_translations", f"Translations of {self.name}", {"product": self.id},
                           dedupe_key=f"translations:{self.id}", source="cron", external_ref=self.shopify_gid, priority=12)

    def _queue_translation_export(self):
        self.ensure_one()
        store = self.instance_id
        if store.sync_translations and store._translation_rows():
            store._enqueue("export_translations", f"Export translations of {self.name}", {"product": self.id},
                           dedupe_key=f"export_translations:{self.id}", source="odoo", external_ref=self.shopify_gid, priority=12)

    def action_import_translations(self):
        for product in self.filtered("shopify_gid"):
            product.instance_id._enqueue("import_translations", f"Translations of {product.name}", {"product": product.id},
                                         dedupe_key=f"translations:{product.id}", external_ref=product.shopify_gid, priority=12)
        return True

    def action_export_translations(self):
        for product in self.filtered("shopify_gid"):
            product.instance_id._enqueue("export_translations", f"Export translations of {product.name}", {"product": product.id},
                                         dedupe_key=f"export_translations:{product.id}", external_ref=product.shopify_gid, priority=12)
        return True

    def _translation_gids(self):
        self.ensure_one()
        gids = [self.shopify_gid]
        for option in self.option_data or []:
            if option.get("id"):
                gids.append(option["id"])
            gids += [value["id"] for value in option.get("optionValues") or [] if value.get("id")]
        return gids

    def _attribute_lookup(self):
        """Odoo attribute and value behind each Shopify option, by their source names."""
        self.ensure_one()
        source = self.instance_id._source_lang()
        result = {}
        template = self.product_tmpl_id.with_context(lang=source)
        for line in template.attribute_line_ids:
            for option in self.option_data or []:
                if option.get("name", "").lower() != line.attribute_id.name.lower():
                    continue
                if option.get("id"):
                    result[option["id"]] = line.attribute_id
                for value in option.get("optionValues") or []:
                    match = line.value_ids.filtered(lambda v: v.name.lower() == value["name"].lower())[:1]
                    if match and value.get("id"):
                        result[value["id"]] = match
        return result

    def _differs(self, record, field, code):
        """The value in ``code`` when it really is a translation, not the source fallback."""
        if not record:
            return None
        source = self.instance_id._source_lang()
        value = record.with_context(lang=code)[field]
        return value if value and value != record.with_context(lang=source)[field] else None

    def _translation_values(self, code):
        """``{resource gid: {shopify key: translated text}}`` for an Odoo language."""
        self.ensure_one()
        template = self.product_tmpl_id
        title = self._differs(self, "name", code) or self._differs(template, "name", code)
        body = self._differs(self, "description_html", code)
        if not body:
            sale = self._differs(template, "description_sale", code)
            body = sale and f"<p>{sale}</p>"
        values = {self.shopify_gid: {
            "title": title,
            "body_html": str(body) if body else None,
            "meta_title": self._differs(self, "seo_title", code),
            "meta_description": self._differs(self, "seo_description", code),
        }}
        for gid, record in self._attribute_lookup().items():
            values[gid] = {"name": self._differs(record, "name", code)}
        return values

    def _apply_translations(self, resources, code):
        """Store Shopify translations. Odoo master data follows the store's overwrite policy."""
        self.ensure_one()
        store = self.instance_id
        count = 0
        fields_map = {"title": "name", "body_html": "description_html", "meta_title": "seo_title", "meta_description": "seo_description"}
        resource = resources.get(self.shopify_gid)
        vals = {}
        for key, field in fields_map.items():
            value = store._translated_value(resource, key)
            if value:
                vals[field] = value
        if vals:
            self.with_context(lang=code).write(vals)
            count += len(vals)
            template = self.product_tmpl_id
            if template and (self.created_by_import or store.update_existing_products):
                write = {}
                if vals.get("name"):
                    write["name"] = vals["name"]
                if vals.get("description_html"):
                    write["description_sale"] = html2plaintext(vals["description_html"])[:2000]
                if write:
                    template.with_context(lang=code).write(write)
        if self.created_by_import or store.update_existing_products:
            for gid, record in self._attribute_lookup().items():
                value = store._translated_value(resources.get(gid), "name")
                # Attributes are shared between products: never replace an existing translation.
                if value and not self._differs(record, "name", code):
                    record.with_context(lang=code).name = value
                    count += 1
        return count

    def action_ai_translate(self):
        """Fill every target language from the source text with the store's LLM, for review."""
        for product in self:
            store = product.instance_id
            if not store._ai_available():
                raise UserError(self.env._("Configure an AI provider on the store first."))
            rows = store._translation_rows()
            if not rows:
                raise UserError(self.env._("Enable at least one language on the store first."))
            source = product.with_context(lang=store._source_lang())
            facts = {
                "title": source.name,
                "description_html": str(source.description_html or ""),
                "seo_title": source.seo_title or "",
                "seo_description": source.seo_description or "",
            }
            for row in rows:
                answer = ai_lib.extract_json(store._ai_complete(
                    "translate",
                    f"Translate e-commerce product copy into {row.name}. Keep every HTML tag, brand name, size and number "
                    "unchanged. Answer only in JSON with the same keys as the input. seo_title stays under 70 characters "
                    "and seo_description under 160.",
                    json.dumps(facts), max_tokens=1500,
                ))
                if not isinstance(answer, dict):
                    raise UserError(self.env._("The AI answer could not be read. Please try again."))
                vals = {"name": answer.get("title"), "description_html": answer.get("description_html"),
                        "seo_title": (answer.get("seo_title") or "")[:70] or False,
                        "seo_description": (answer.get("seo_description") or "")[:160] or False}
                product.with_context(lang=row.lang_id.code).write({key: value for key, value in vals.items() if value})
        return True
