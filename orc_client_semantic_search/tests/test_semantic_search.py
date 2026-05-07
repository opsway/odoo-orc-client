"""Tests for the public ``orc.embedding.semantic_search`` method —
the one the ORC agent calls via XML-RPC.

The contract (README "API surface"):
- Returns ``[{model, id, score}]`` — refs only.
- Sorted descending by score.
- Honours ``limit`` (default 10, max 50).
- Filters to enabled config rows by default; optional ``models``
  arg restricts further.
- Raises ``UserError`` on missing config, missing provider key,
  empty corpus, or empty query.
"""
from unittest.mock import MagicMock, patch

import numpy as np
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _norm(v):
    arr = np.array(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class SemanticSearchContractTests(TransactionCase):
    def setUp(self):
        super().setUp()
        self.config = self.env["orc.embedding.config"].search(
            [("is_global", "=", True)], limit=1,
        )
        self.config.write({
            "provider_api_key": "sk-test",
            "vector_dim": 4,
        })

        # Seed three articles with distinct vectors. The cron path
        # is exercised in test_indexing_lifecycle; here we write
        # orc.embedding rows directly so the test focuses on
        # search behaviour.
        Embedding = self.env["orc.embedding"]
        Article = self.env["knowledge.article"]

        self.a1 = Article.create({"name": "Aligned", "body": "<p>x</p>"})
        self.a2 = Article.create({"name": "Orthogonal", "body": "<p>y</p>"})
        self.a3 = Article.create({"name": "Opposite", "body": "<p>z</p>"})

        # Wipe the queue rows the create-hook just enqueued so the
        # cron doesn't fight us.
        self.env["orc.embedding.queue"].search([]).unlink()
        Embedding.search([]).unlink()

        Embedding.create([
            {
                "model": "knowledge.article", "res_id": self.a1.id,
                "vector_blob": _norm([1.0, 0.0, 0.0, 0.0]).tobytes(),
                "content_hash": "h1",
                "indexed_at": "2026-05-07 00:00:00",
                "provider": "openai:text-embedding-3-small",
                "text_excerpt_len": 10,
            },
            {
                "model": "knowledge.article", "res_id": self.a2.id,
                "vector_blob": _norm([0.0, 1.0, 0.0, 0.0]).tobytes(),
                "content_hash": "h2",
                "indexed_at": "2026-05-07 00:00:00",
                "provider": "openai:text-embedding-3-small",
                "text_excerpt_len": 10,
            },
            {
                "model": "knowledge.article", "res_id": self.a3.id,
                "vector_blob": _norm([-1.0, 0.0, 0.0, 0.0]).tobytes(),
                "content_hash": "h3",
                "indexed_at": "2026-05-07 00:00:00",
                "provider": "openai:text-embedding-3-small",
                "text_excerpt_len": 10,
            },
        ])

        self.provider = MagicMock()
        self.provider.dim = 4
        self.provider.provider_tag.return_value = "openai:text-embedding-3-small"
        # Query embedding aligns with a1.
        self.provider.embed.return_value = [_norm([1.0, 0.0, 0.0, 0.0]).tolist()]

    def _patch_provider(self):
        return patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=self.provider,
        )

    def test_returns_refs_only(self):
        # No title, no snippet, no body — the README's permission
        # claim depends on this. If a future change starts leaking
        # titles, this test breaks the build.
        with self._patch_provider():
            out = self.env["orc.embedding"].semantic_search(
                "anything",
            )
        self.assertGreater(len(out), 0)
        for row in out:
            self.assertEqual(set(row.keys()), {"model", "id", "score"})

    def test_sorted_descending_by_score(self):
        with self._patch_provider():
            out = self.env["orc.embedding"].semantic_search(
                "anything",
            )
        scores = [r["score"] for r in out]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_hit_is_the_aligned_article(self):
        with self._patch_provider():
            out = self.env["orc.embedding"].semantic_search(
                "anything", limit=1,
            )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["model"], "knowledge.article")
        self.assertEqual(out[0]["id"], self.a1.id)
        self.assertAlmostEqual(out[0]["score"], 1.0, places=5)

    def test_respects_limit(self):
        with self._patch_provider():
            out = self.env["orc.embedding"].semantic_search(
                "anything", limit=2,
            )
        self.assertEqual(len(out), 2)

    def test_clamps_limit_to_max(self):
        # Per README, limit caps at 50. A request for 1000 should
        # silently clamp, not raise.
        with self._patch_provider():
            out = self.env["orc.embedding"].semantic_search(
                "anything", limit=1000,
            )
        self.assertLessEqual(len(out), 50)

    def test_models_filter_restricts_corpus(self):
        with self._patch_provider():
            out = self.env["orc.embedding"].semantic_search(
                "anything", models=["res.partner"],
            )
        # No res.partner rows seeded → empty result, not error.
        self.assertEqual(out, [])

    def test_missing_provider_key_raises_user_error(self):
        self.config.provider_api_key = False
        with self.assertRaises(UserError):
            self.env["orc.embedding"].semantic_search("anything")

    def test_empty_query_raises_user_error(self):
        with self.assertRaises(UserError):
            self.env["orc.embedding"].semantic_search("")

    def test_provider_error_raises_user_error(self):
        # Per README "Failure modes": provider errors raise a clean
        # UserError so odoo-mcp surfaces them as a tool error and
        # the agent falls back per-turn.
        from odoo.addons.orc_client_semantic_search.providers.base import (
            EmbeddingProviderError,
        )
        self.provider.embed.side_effect = EmbeddingProviderError("503", status=503)
        with self._patch_provider():
            with self.assertRaises(UserError):
                self.env["orc.embedding"].semantic_search("anything")
