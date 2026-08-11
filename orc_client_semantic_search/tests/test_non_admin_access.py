"""The indexing hooks must be invisible to the end user.

``orc.embedding*`` are technical models gated to
``base.group_system`` (README "Permission model"). A plain internal
user editing a knowledge article must not be asked for rights on
them — before the bookkeeping was sudo'd, creating an article raised
"You are not allowed to access 'orc.embedding.queue' records".

The same applies to ``semantic_search``: the agent calls it as the
end user, who cannot read the provider config row.
"""
from unittest.mock import patch

import numpy as np
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.orc_client_semantic_search.providers.openai import (
    OpenAIEmbeddingProvider,
)


_VECTOR = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes()


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class NonAdminAccessTests(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env["orc.embedding.config"].search(
            [("is_global", "=", True)], limit=1,
        )
        cls.config.write({"provider_api_key": "sk-test", "vector_dim": 4})

        # A plain internal user: no Settings access, so no rights on
        # any of the orc.embedding* models.
        cls.user = cls.env["res.users"].create({
            "name": "Semantic Search Editor",
            "login": "orc_semantic_editor",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })

    def _queue_count(self, article_id):
        # Counted as the superuser — the test user can't read the queue.
        return self.env["orc.embedding.queue"].sudo().search_count([
            ("model", "=", "knowledge.article"), ("res_id", "=", article_id),
        ])

    def test_internal_user_can_author_articles(self):
        Article = self.env["knowledge.article"].with_user(self.user)

        article = Article.create({
            "name": "Written by a regular employee",
            "body": "<p>first draft</p>",
        })
        self.assertEqual(
            self._queue_count(article.id), 1,
            "create() by a non-admin must still enqueue a re-index marker",
        )

        # Body is the watched field for knowledge.article, so this
        # goes down the enqueue path rather than the early return.
        article.write({"body": "<p>second draft</p>"})
        self.assertEqual(
            self._queue_count(article.id), 1,
            "re-enqueueing an already-queued record must stay a no-op",
        )

        # Deleting is a soft archive for an internal user (knowledge
        # keeps unlink for the system group + its trash cron), and it
        # runs through the same write hook.
        article.action_send_to_trash()
        self.assertFalse(article.active)

    def test_semantic_search_runs_as_the_end_user(self):
        article = self.env["knowledge.article"].create({
            "name": "Indexed", "body": "<p>x</p>",
        })
        self.env["orc.embedding.queue"].sudo().search([]).unlink()
        self.env["orc.embedding"].sudo().create({
            "model": "knowledge.article",
            "res_id": article.id,
            "vector_blob": _VECTOR,
            "content_hash": "h1",
            "indexed_at": "2026-05-07 00:00:00",
            "provider": "openai:text-embedding-3-small",
            "text_excerpt_len": 10,
        })

        # The provider is built from the config row inside the call,
        # so patch the HTTP layer rather than _build_provider — that
        # keeps the config read under test.
        with patch.object(
            OpenAIEmbeddingProvider, "embed",
            return_value=[[1.0, 0.0, 0.0, 0.0]],
        ):
            hits = self.env["orc.embedding"].with_user(
                self.user,
            ).semantic_search("anything")

        self.assertEqual(
            [h["id"] for h in hits], [article.id],
            "a non-admin caller must get the same refs an admin does",
        )
