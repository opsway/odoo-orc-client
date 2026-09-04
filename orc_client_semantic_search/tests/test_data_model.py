"""Tests for the data model fields and constraints documented in
README "Data model"."""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged
from psycopg2 import IntegrityError
from psycopg2.errorcodes import EXCLUSION_VIOLATION, UNIQUE_VIOLATION


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class OrcEmbeddingFieldsTests(TransactionCase):
    def test_required_fields_present(self):
        Embedding = self.env["orc.embedding"]
        # Sanity-check the field set against README "Data model".
        # If a field is dropped or renamed, downstream callers break;
        # this catches it at install.
        for field in (
            "model", "res_id", "vector_blob", "content_hash",
            "text_excerpt_len", "indexed_at", "provider",
        ):
            self.assertIn(field, Embedding._fields, f"missing field: {field}")

    def test_unique_model_res_id(self):
        Embedding = self.env["orc.embedding"]
        Embedding.create({
            "model": "knowledge.article", "res_id": 1,
            "content_hash": "a" * 64,
        })
        with self.assertRaises(IntegrityError) as ctx:
            with self.cr.savepoint():
                Embedding.create({
                    "model": "knowledge.article", "res_id": 1,
                    "content_hash": "b" * 64,
                })
        # Confirm it's the unique constraint, not some other DB error.
        self.assertEqual(ctx.exception.pgcode, UNIQUE_VIOLATION)


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class OrcEmbeddingConfigSingletonTests(TransactionCase):
    def test_demo_data_creates_global_row(self):
        # The data file ships one global row pre-seeded so the
        # Settings page renders something on first install.
        Config = self.env["orc.embedding.config"]
        globals_ = Config.search([("is_global", "=", True)])
        self.assertEqual(len(globals_), 1)

    def test_cannot_create_second_global_row(self):
        Config = self.env["orc.embedding.config"]
        with self.assertRaises(IntegrityError) as ctx:
            with self.cr.savepoint():
                Config.create({
                    "is_global": True,
                    "provider_kind": "openai",
                    "provider_url": "https://x",
                    "provider_api_key": "sk-x",
                    "provider_model": "text-embedding-3-small",
                    "vector_dim": 1536,
                })
        self.assertEqual(ctx.exception.pgcode, EXCLUSION_VIOLATION)

    def test_global_row_must_not_set_model_name(self):
        # Catches mis-configuration: a "global" row with a model_name
        # would be ambiguous (provider config or per-model toggle?).
        #
        # Deliberately a model NO config row already claims, asserted as a
        # precondition below, so the test can only be satisfied by the
        # Python `_check_row_kind_fields` it is named after.
        #
        # The precondition is asserted rather than the exception message:
        # that message goes through `_()` and i18n/pl.po ships a live
        # Polish translation of it, so pinning the English text would fail
        # under a pl_PL test context on a perfectly working constraint.
        #
        # This used to write the indexed model's own name, which a config row
        # already holds. On 18.0 that still reached the Python constraint
        # first (verified: the ValidationError carries this message, and
        # neutering the guard turns the test red, not green) — but it left
        # the outcome one seeded row away from being decided by the SQL
        # `UNIQUE (model_name)` instead, which surfaces as an unrelated
        # IntegrityError. INT-1066 reports the SQL constraint winning on
        # 15.0, which is where this was found.
        Config = self.env["orc.embedding.config"]
        unclaimed = "res.partner"
        self.assertFalse(
            Config.search_count([("model_name", "=", unclaimed)]),
            "%s must stay unclaimed, or the SQL UNIQUE (model_name) fires "
            "instead of the Python constraint this test is named after"
            % unclaimed,
        )
        existing = Config.search([("is_global", "=", True)], limit=1)
        with self.assertRaises(ValidationError):
            existing.write({"model_name": unclaimed})


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class OrcEmbeddingConfigPerModelTests(TransactionCase):
    def test_demo_data_creates_knowledge_article_row(self):
        Config = self.env["orc.embedding.config"]
        rows = Config.search([
            ("is_global", "=", False),
            ("model_name", "=", "knowledge.article"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.text_field_path, "body")
        self.assertEqual(rows.text_extractor, "html_strip")

    def test_per_model_row_must_have_model_name(self):
        Config = self.env["orc.embedding.config"]
        with self.assertRaises(ValidationError):
            Config.create({"is_global": False})  # model_name missing

    def test_unique_per_model_name(self):
        Config = self.env["orc.embedding.config"]
        with self.assertRaises(IntegrityError) as ctx:
            with self.cr.savepoint():
                Config.create({
                    "is_global": False,
                    "model_name": "knowledge.article",  # already in demo data
                    "text_field_path": "body",
                    "text_extractor": "html_strip",
                })
        self.assertEqual(ctx.exception.pgcode, UNIQUE_VIOLATION)

    def test_per_model_row_must_not_set_provider_fields(self):
        Config = self.env["orc.embedding.config"]
        with self.assertRaises(ValidationError):
            Config.create({
                "is_global": False,
                "model_name": "res.partner",
                "provider_kind": "openai",  # forbidden on per-model row
            })


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class OrcEmbeddingQueueFieldsTests(TransactionCase):
    def test_required_fields_present(self):
        Queue = self.env["orc.embedding.queue"]
        for field in ("model", "res_id", "enqueued_at", "attempts", "last_error"):
            self.assertIn(field, Queue._fields)

    def test_unique_model_res_id(self):
        Queue = self.env["orc.embedding.queue"]
        Queue.create({"model": "knowledge.article", "res_id": 1})
        with self.assertRaises(IntegrityError) as ctx:
            with self.cr.savepoint():
                Queue.create({"model": "knowledge.article", "res_id": 1})
        self.assertEqual(ctx.exception.pgcode, UNIQUE_VIOLATION)
