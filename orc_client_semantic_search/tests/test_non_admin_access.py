"""The indexing hooks must be invisible to the end user.

``orc.embedding*`` are technical models gated to
``base.group_system`` (README "Permission model"). A plain internal
user editing a wiki page must not be asked for rights on them —
before the bookkeeping was sudo'd, creating a page raised
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
        assert cls.config, "the module data must ship a global config row"
        cls.config.write({"provider_api_key": "sk-test", "vector_dim": 4})

        # A plain internal user with document_page's own Manager group and
        # nothing else. `base.group_user` alone cannot author a page on v15 —
        # document_page's ACLs grant write/create to Editor and unlink to
        # Manager — and Manager is what the unlink assertion below needs.
        # Crucially it does NOT imply base.group_system, so the user still has
        # no rights on any orc.embedding* model, which is the whole point.
        cls.user = cls.env["res.users"].create({
            "name": "Semantic Search Editor",
            "login": "orc_semantic_editor",
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("document_page.group_document_manager").id,
            ])],
        })

    def _queue_count(self, article_id):
        # Counted as the superuser — the test user can't read the queue.
        return self.env["orc.embedding.queue"].sudo().search_count([
            ("model", "=", "document.page"), ("res_id", "=", article_id),
        ])

    def test_internal_user_can_author_pages(self):
        Article = self.env["document.page"].with_user(self.user)

        article = Article.create({
            "name": "Written by a regular employee",
            "content": "<p>first draft</p>",
        })
        self.assertEqual(
            self._queue_count(article.id), 1,
            "create() by a non-admin must still enqueue a re-index marker",
        )

        # `content` is the watched field for document.page, so this goes
        # down the enqueue path rather than the early return. It is a
        # computed field with an inverse, but an edit still arrives in
        # `vals` under that name.
        article.write({"content": "<p>second draft</p>"})
        self.assertEqual(
            self._queue_count(article.id), 1,
            "re-enqueueing an already-queued record must stay a no-op",
        )

        # Deleting is a real unlink on v15 (document_page has no trash
        # cron; Manager holds the unlink right), and it runs through the
        # `unlink` hook — which is the stronger case for this test, because
        # that hook is the one that reaches into orc.embedding* to drop the
        # rows. A non-admin must not be asked for rights to do it.
        article_id = article.id
        article.unlink()
        self.assertEqual(self._queue_count(article_id), 0)

    def test_semantic_search_runs_as_the_end_user(self):
        article = self.env["document.page"].create({
            "name": "Indexed", "content": "<p>x</p>",
        })
        # Start from an empty index so the ranking below only has
        # the row seeded here to work with.
        self.env["orc.embedding.queue"].sudo().search([]).unlink()
        self.env["orc.embedding"].sudo().search([]).unlink()
        self.env["orc.embedding"].sudo().create({
            "model": "document.page",
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
