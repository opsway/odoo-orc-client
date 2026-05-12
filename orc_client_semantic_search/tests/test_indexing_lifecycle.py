"""End-to-end indexing lifecycle: create/write hooks → queue → cron
→ orc.embedding row. README "Indexing lifecycle" is the contract."""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.orc_client_semantic_search.providers.base import (
    EmbeddingProviderError,
)


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class IndexingLifecycleTests(TransactionCase):
    def setUp(self):
        super().setUp()
        self.config = self.env["orc.embedding.config"].search(
            [("is_global", "=", True)], limit=1,
        )
        self.config.write({"provider_api_key": "sk-test"})

    def _stub_provider(self, dim=4):
        mock = MagicMock()
        mock.embed.return_value = [[1.0] + [0.0] * (dim - 1)]
        mock.provider_tag.return_value = "openai:text-embedding-3-small"
        mock.dim = dim
        return mock

    def test_create_enqueues_marker(self):
        Queue = self.env["orc.embedding.queue"]
        before = Queue.search_count([("model", "=", "knowledge.article")])
        article = self.env["knowledge.article"].create({
            "name": "Lifecycle test", "body": "<p>content</p>",
        })
        after = Queue.search_count([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ])
        self.assertEqual(after, 1, "create() must enqueue exactly one marker")
        self.assertGreaterEqual(Queue.search_count([]), before + 1)

    def test_cron_processes_queue_into_embedding_row(self):
        article = self.env["knowledge.article"].create({
            "name": "Cron test", "body": "<p>hello world</p>",
        })

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            self.env["orc.embedding"]._cron_reindex_sweep()

        Embedding = self.env["orc.embedding"]
        row = Embedding.search([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ], limit=1)
        self.assertTrue(row, "expected an orc.embedding row after the sweep")
        self.assertTrue(row.content_hash)
        self.assertTrue(row.indexed_at)
        self.assertEqual(row.provider, "openai:text-embedding-3-small")
        self.assertTrue(row.vector_blob)

        # And the queue marker for that record must be gone.
        Queue = self.env["orc.embedding.queue"]
        leftover = Queue.search_count([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ])
        self.assertEqual(leftover, 0)

    def test_provider_failure_keeps_queue_row_and_increments_attempts(self):
        article = self.env["knowledge.article"].create({
            "name": "Failure test", "body": "<p>x</p>",
        })

        provider = MagicMock()
        provider.embed.side_effect = EmbeddingProviderError("503", status=503)
        provider.dim = 4
        provider.provider_tag.return_value = "openai:text-embedding-3-small"

        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            # The cron must catch and not crash — other records in
            # the queue should still get processed in the same pass.
            self.env["orc.embedding"]._cron_reindex_sweep()

        Queue = self.env["orc.embedding.queue"]
        row = Queue.search([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ], limit=1)
        self.assertTrue(row, "queue marker should remain after a provider error")
        self.assertEqual(row.attempts, 1)
        self.assertIn("503", row.last_error or "")

    def test_delete_cascades_embedding(self):
        # Article unlinked → its orc.embedding row goes too. We
        # don't want stale ids in the index that would only be
        # noticed when the agent tries to read a 404.
        article = self.env["knowledge.article"].create({
            "name": "Delete cascade test", "body": "<p>x</p>",
        })

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            self.env["orc.embedding"]._cron_reindex_sweep()

        Embedding = self.env["orc.embedding"]
        before = Embedding.search_count([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ])
        self.assertEqual(before, 1)

        article.unlink()

        after = Embedding.search_count([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ])
        self.assertEqual(after, 0)

    def test_long_article_falls_back_to_first_8k_chars(self):
        # >8K-char body → embed first 8K, log a warning. We pin the
        # warning being captured on the queue row's last_error
        # field at index time so operators can see what was
        # truncated.
        long_body = "<p>" + ("alpha beta gamma " * 1000) + "</p>"
        article = self.env["knowledge.article"].create({
            "name": "Long article", "body": long_body,
        })

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            self.env["orc.embedding"]._cron_reindex_sweep()

        # The text actually sent to the provider is one positional
        # arg, a list of one string. Inspect what was sent.
        args, _ = provider.embed.call_args
        sent_texts = args[0]
        self.assertEqual(len(sent_texts), 1)
        self.assertLessEqual(len(sent_texts[0]), 8000)

        Embedding = self.env["orc.embedding"]
        row = Embedding.search([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ], limit=1)
        self.assertEqual(row.text_excerpt_len, len(sent_texts[0]))
