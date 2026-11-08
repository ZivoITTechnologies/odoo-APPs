import json

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import ZivoShopifyCommon
from .shopify_mock import make_collection, make_product, make_variant


def sized_product(gid="100", collections=None):
    options = [{"name": "Size", "position": 1, "values": ["Small", "Large"]}]
    return make_product(gid, "Runner Shoe", [
        make_variant(f"{gid}1", f"RUN-{gid}-S", "50.00", [{"name": "Size", "value": "Small"}], "Small"),
        make_variant(f"{gid}2", f"RUN-{gid}-L", "55.00", [{"name": "Size", "value": "Large"}], "Large", position=2),
    ], options, collections=collections)


@tagged("post_install", "-at_install")
class TestCollections(ZivoShopifyCommon):

    def import_product(self, node):
        self.shop.products[node["id"]] = node
        self.instance.action_import_products()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        return self.env["zivo.shopify.product"].search([("instance_id", "=", self.instance.id), ("shopify_gid", "=", node["id"])])

    def import_collections(self):
        self.instance.action_import_collections()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        return self.instance.collection_ids

    def test_import_creates_categories(self):
        self.shop.collections = [make_collection("1", "Winter Shoes"), make_collection("2", "All Products", smart=True)]
        records = self.import_collections()
        manual = records.filtered(lambda c: c.name == "Winter Shoes")
        self.assertEqual(manual.collection_type, "manual")
        self.assertEqual(manual.category_id.name, "Winter Shoes")
        self.assertEqual(manual.category_id.parent_id.name, "Shopify Collections")
        self.assertTrue(manual.category_created)
        self.assertEqual(records.filtered(lambda c: c.name == "All Products").collection_type, "smart")

    def test_similar_category_is_reused(self):
        existing = self.env["product.category"].create({"name": "Winter-Shoes"})
        self.shop.collections = [make_collection("1", "Winter Shoes")]
        record = self.import_collections()
        self.assertEqual(record.category_id, existing)
        self.assertFalse(record.category_created)

    def test_no_category_when_disabled(self):
        self.instance.auto_create_categories = False
        self.shop.collections = [make_collection("1", "Zzyzx Unique")]
        self.assertFalse(self.import_collections().category_id)

    def test_manual_mapping_survives_reimport(self):
        chosen = self.env["product.category"].create({"name": "Chosen"})
        self.shop.collections = [make_collection("1", "Winter Shoes")]
        record = self.import_collections()
        record.category_id = chosen
        self.shop.collections[0]["title"] = "Winter Boots"
        self.import_collections()
        self.assertEqual(record.name, "Winter Boots")
        self.assertEqual(record.category_id, chosen)

    def test_product_gets_category_of_manual_collection(self):
        collections = [make_collection("2", "All Products", smart=True), make_collection("1", "Running")]
        record = self.import_product(sized_product(collections=collections))
        self.assertEqual(record.product_tmpl_id.categ_id.name, "Running")
        self.assertEqual(len(record.collection_ids), 2)

    def test_matched_product_keeps_its_category(self):
        category = self.env["product.category"].create({"name": "Mine"})
        product = self.env["product.product"].create({"name": "X", "default_code": "ONE-1", "categ_id": category.id})
        self.import_product(make_product("300", "Y", [make_variant("301", "ONE-1")], collections=[make_collection("1", "Running")]))
        self.assertEqual(product.categ_id, category)

    def test_overwrite_policy_moves_matched_product(self):
        self.instance.update_existing_products = True
        product = self.env["product.product"].create({"name": "X", "default_code": "ONE-1"})
        self.import_product(make_product("300", "Y", [make_variant("301", "ONE-1")], collections=[make_collection("1", "Running")]))
        self.assertEqual(product.categ_id.name, "Running")

    def test_export_sends_only_manual_collections(self):
        collections = [make_collection("2", "All Products", smart=True), make_collection("1", "Running")]
        record = self.import_product(sized_product(collections=collections))
        built = record._build_product_set_input()
        self.assertEqual(built["collections"], ["gid://shopify/Collection/1"])

    def test_export_maps_category_to_collection(self):
        self.shop.collections = [make_collection("7", "Bags")]
        collection = self.import_collections()
        template = self.env["product.template"].create({"name": "Tote", "categ_id": collection.category_id.id})
        record = self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)
        record._export_to_shopify()
        self.assertEqual(self.shop.ops("ZivoProductSet")[0][1]["input"]["collections"], ["gid://shopify/Collection/7"])

    def test_category_becomes_collection_when_enabled(self):
        self.instance.export_categories = True
        category = self.env["product.category"].create({"name": "Lamps"})
        template = self.env["product.template"].create({"name": "Desk Lamp", "categ_id": category.id})
        record = self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)
        record._export_to_shopify()
        self.assertEqual(self.shop.ops("ZivoCollectionCreate")[0][1]["collection"]["title"], "Lamps")
        mapping = self.instance.collection_ids.filtered(lambda c: c.category_id == category)
        self.assertEqual(self.shop.ops("ZivoProductSet")[0][1]["input"]["collections"], [mapping.shopify_gid])
        record._export_to_shopify()
        self.assertEqual(len(self.shop.ops("ZivoCollectionCreate")), 1, "created once")

    def test_no_collection_created_by_default(self):
        template = self.env["product.template"].create({"name": "Desk Lamp"})
        self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)._export_to_shopify()
        self.assertFalse(self.shop.ops("ZivoCollectionCreate"))

    def test_collection_webhooks(self):
        self.shop.collections = [make_collection("1", "Running")]
        record = self.import_collections()
        Webhook = self.env["zivo.shopify.webhook"]
        Webhook.search([("instance_id", "=", self.instance.id), ("topic", "=", "COLLECTIONS_DELETE")]).enabled = True
        Webhook._dispatch(self.instance, "COLLECTIONS_DELETE", {"id": 1})
        self.assertFalse(record.exists())
        Webhook._dispatch(self.instance, "COLLECTIONS_UPDATE", {"id": 2})
        self.assertTrue(self.jobs("pending").filtered(lambda j: j.job_type == "import_collections"))


@tagged("post_install", "-at_install")
class TestLanguages(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.shop.locales.append({"locale": "fr", "name": "French", "primary": False, "published": True})
        self.instance._sync_languages()
        self.french = self.instance.language_ids.filtered(lambda l: l.locale == "fr")
        self.instance.sync_translations = True

    def import_product(self, node=None):
        node = node or sized_product()
        self.shop.products[node["id"]] = node
        self.instance.action_import_products()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        return self.env["zivo.shopify.product"].search([("instance_id", "=", self.instance.id), ("shopify_gid", "=", node["id"])]), node

    def seed_french(self, node):
        self.shop.translations[(node["id"], "fr")] = {"title": "Chaussure de course", "body_html": "<p>Description</p>", "meta_title": "Course", "meta_description": "Rapide"}
        option = node["options"][0]
        self.shop.translations[(option["id"], "fr")] = {"name": "Taille"}
        self.shop.translations[(option["optionValues"][0]["id"], "fr")] = {"name": "Petit"}

    def test_languages_are_mapped(self):
        self.assertEqual(self.french.lang_id.code, "fr_FR")
        self.assertTrue(self.french.sync)
        english = self.instance.language_ids.filtered("primary")
        self.assertEqual(english.lang_id.code, "en_US")
        self.assertFalse(english.sync)

    def test_regional_locale(self):
        self.assertEqual(self.env["zivo.shopify.language"]._find_odoo_language("pt-BR").code, "pt_BR")

    def test_unknown_locale_is_not_synced(self):
        self.shop.locales.append({"locale": "xx", "name": "Nowhere", "primary": False, "published": True})
        self.instance._sync_languages()
        row = self.instance.language_ids.filtered(lambda l: l.locale == "xx")
        self.assertFalse(row.lang_id)
        self.assertFalse(row.sync)

    def test_import_translates_product_and_options(self):
        node = sized_product()
        self.seed_french(node)
        record, _ = self.import_product(node)
        french = record.with_context(lang="fr_FR")
        self.assertEqual(french.name, "Chaussure de course")
        self.assertEqual(record.with_context(lang="en_US").name, "Runner Shoe", "source text untouched")
        self.assertEqual(french.seo_title, "Course")
        template = record.product_tmpl_id.with_context(lang="fr_FR")
        self.assertEqual(template.name, "Chaussure de course")
        self.assertEqual(template.description_sale, "Description")
        attribute = template.attribute_line_ids.attribute_id
        self.assertEqual(attribute.name, "Taille")
        self.assertIn("Petit", template.attribute_line_ids.value_ids.mapped("name"))

    def test_matched_odoo_product_is_not_retranslated(self):
        product = self.env["product.product"].create({"name": "Own name", "default_code": "ONE-1"})
        node = make_product("300", "Y", [make_variant("301", "ONE-1")])
        self.shop.translations[(node["id"], "fr")] = {"title": "Autre"}
        record, _ = self.import_product(node)
        self.assertEqual(record.with_context(lang="fr_FR").name, "Autre")
        self.assertEqual(product.product_tmpl_id.with_context(lang="fr_FR").name, "Own name")

    def test_outdated_translation_is_skipped(self):
        node = sized_product()
        self.seed_french(node)
        self.shop.outdated.add((node["id"], "title"))
        record, _ = self.import_product(node)
        self.assertEqual(record.with_context(lang="fr_FR").name, "Runner Shoe")

    def test_existing_attribute_translation_is_kept(self):
        self.instance._translation_rows()
        attribute = self.env["product.attribute"].create({"name": "Size", "create_variant": "always"})
        attribute.with_context(lang="fr_FR").name = "Pointure"
        node = sized_product()
        self.seed_french(node)
        self.import_product(node)
        self.assertEqual(attribute.with_context(lang="fr_FR").name, "Pointure")

    def test_import_not_queued_when_off(self):
        self.instance.sync_translations = False
        self.import_product()
        self.assertFalse(self.jobs().filtered(lambda j: j.job_type == "import_translations"))

    def test_export_sends_translations_with_digest(self):
        record, node = self.import_product()
        record.with_context(lang="fr_FR").write({"name": "Basket", "seo_title": "Titre"})
        record.action_export_translations()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        self.assertEqual(self.shop.translations[(node["id"], "fr")], {"title": "Basket", "meta_title": "Titre"})
        sent = self.shop.ops("ZivoTranslationsRegister")[0][1]["translations"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["locale"], "fr")
        self.assertTrue(sent[0]["translatableContentDigest"])

    def test_untranslated_text_is_never_sent(self):
        record, _ = self.import_product()
        record.action_export_translations()
        self.run_queue()
        self.assertFalse(self.shop.ops("ZivoTranslationsRegister"))

    def test_odoo_template_and_attribute_translations_are_exported(self):
        record, node = self.import_product()
        template = record.product_tmpl_id
        template.with_context(lang="fr_FR").name = "Nom modèle"
        line = template.attribute_line_ids
        line.attribute_id.with_context(lang="fr_FR").name = "Taille"
        line.value_ids[0].with_context(lang="fr_FR").name = "Petit"
        record.action_export_translations()
        self.run_queue()
        option = node["options"][0]
        self.assertEqual(self.shop.translations[(node["id"], "fr")]["title"], "Nom modèle")
        self.assertEqual(self.shop.translations[(option["id"], "fr")]["name"], "Taille")
        self.assertEqual(self.shop.translations[(option["optionValues"][0]["id"], "fr")]["name"], "Petit")

    def test_product_export_queues_translations(self):
        template = self.env["product.template"].create({"name": "Lamp"})
        self.env["zivo.shopify.product"]._ensure_for_template(self.instance, template)._export_to_shopify()
        self.assertTrue(self.jobs("pending").filtered(lambda j: j.job_type == "export_translations"))

    def test_primary_language_other_than_english(self):
        self.shop.locales[0].update(locale="de", name="German")
        self.instance.language_ids.filtered("primary").unlink()
        self.instance._sync_languages()
        german = self.instance.language_ids.filtered(lambda l: l.locale == "de")
        self.assertEqual(self.instance._source_lang(), german.lang_id.code)
        record, _ = self.import_product()
        self.assertEqual(record.with_context(lang=german.lang_id.code).name, "Runner Shoe")

    def test_collection_title_translation_is_stored(self):
        self.shop.collections = [make_collection("1", "Running")]
        self.instance.action_import_collections()
        self.shop.translations[("gid://shopify/Collection/1", "fr")] = {"title": "Course à pied"}
        self.run_queue()
        collection = self.instance.collection_ids
        self.assertEqual(collection.with_context(lang="fr_FR").name, "Course à pied")
        self.assertEqual(collection.with_context(lang="en_US").name, "Running")

    def test_collection_title_export(self):
        self.shop.collections = [make_collection("1", "Running")]
        self.instance.action_import_collections()
        self.run_queue()
        self.instance.collection_ids.with_context(lang="fr_FR").name = "Footing"
        job = self.instance._enqueue("export_translations", "c", {"collections": True})
        self.run_queue()
        self.assertEqual(job.state, "done", job.error_message)
        self.assertEqual(self.shop.translations[("gid://shopify/Collection/1", "fr")], {"title": "Footing"})

    def test_ai_translate(self):
        record, _ = self.import_product()
        with self.assertRaises(UserError):
            record.action_ai_translate()
        self.instance.write({"ai_enabled": True, "ai_provider": "openai", "ai_api_key": "k"})
        self.shop.llm_answer = json.dumps({"title": "Chaussure", "description_html": "<p>Belle</p>", "seo_title": "T" * 90, "seo_description": "D"})
        record.action_ai_translate()
        french = record.with_context(lang="fr_FR")
        self.assertEqual(french.name, "Chaussure")
        self.assertEqual(len(french.seo_title), 70)
        self.assertEqual(record.with_context(lang="en_US").name, "Runner Shoe")
        body = json.dumps(self.shop.llm_requests[-1][3])
        self.assertIn("Runner Shoe", body)

    def test_locale_webhook_refreshes_languages(self):
        self.shop.locales.append({"locale": "de", "name": "German", "primary": False, "published": True})
        Webhook = self.env["zivo.shopify.webhook"]
        self.instance._ensure_webhook_rows()
        Webhook._dispatch(self.instance, "LOCALES_CREATE", {"locale": "de"})
        self.assertTrue(self.instance.language_ids.filtered(lambda l: l.locale == "de"))
