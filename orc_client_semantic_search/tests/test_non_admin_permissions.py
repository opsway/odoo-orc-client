"""Regression (GF-295): a non-admin internal user must be able to
create, write, and delete a knowledge.article without hitting an
AccessError on the admin-only index models
(orc.embedding.config / orc.embedding.queue / orc.embedding).

The index bookkeeping is a system side effect of an operation the
user is already allowed to perform, so the knowledge.article hooks
run it with sudo(). Before the fix, any user outside
Administration/Settings was blocked the moment they touched an
article."""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, new_test_user, tagged


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class NonAdminPermissionsTests(TransactionCase):
    def setUp(self):
        super().setUp()
        # A plain internal user: base.group_user, explicitly NOT in
        # Administration/Settings (base.group_system).
        self.user = new_test_user(
            self.env,
            login="gf295_nonadmin",
            groups="base.group_user",
        )
        self.assertFalse(
            self.user.has_group("base.group_system"),
            "test precondition: the user must not be a Settings admin",
        )
        # Provider creds on the global config so the sweep is usable;
        # the sweep itself is patched below so no HTTP is made.
        self.env["orc.embedding.config"].search(
            [("is_global", "=", True)], limit=1,
        ).write({"provider_api_key": "sk-test"})

        self.Article = self.env["knowledge.article"]
        # Queue is admin-only, so verifications read it as admin
        # (self.env), never as self.user.
        self.Queue = self.env["orc.embedding.queue"]

    def _stub_provider(self, dim=4):
        mock = MagicMock()
        mock.embed.return_value = [[1.0] + [0.0] * (dim - 1)]
        mock.provider_tag.return_value = "openai:text-embedding-3-small"
        mock.dim = dim
        return mock

    def test_non_admin_create_enqueues_without_access_error(self):
        # create() -> _orc_enqueue_reindex() touches the admin-only
        # queue. Must not raise for a non-admin author.
        article = self.Article.with_user(self.user).create({
            "name": "GF-295 create", "body": "<p>content</p>",
        })
        self.assertEqual(
            self.Queue.search_count([
                ("model", "=", "knowledge.article"),
                ("res_id", "=", article.id),
            ]),
            1,
            "create() by a non-admin must still enqueue exactly one marker",
        )

    def test_non_admin_write_enqueues_without_access_error(self):
        article = self.Article.with_user(self.user).create({
            "name": "GF-295 write", "body": "<p>before</p>",
        })
        # write() reads the admin-only orc.embedding.config AND
        # enqueues on the admin-only queue. Editing the indexed field
        # as a non-admin must not raise on either.
        article.with_user(self.user).write({"body": "<p>after</p>"})
        self.assertEqual(
            self.Queue.search_count([
                ("model", "=", "knowledge.article"),
                ("res_id", "=", article.id),
            ]),
            1,
        )

    def test_non_admin_unlink_cleans_index_without_access_error(self):
        article = self.Article.with_user(self.user).create({
            "name": "GF-295 unlink", "body": "<p>x</p>",
        })
        # Materialise a real orc.embedding row so unlink() exercises
        # the embedding-cleanup path (also admin-only) too.
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=self._stub_provider(),
        ):
            self.env["orc.embedding"]._cron_reindex_sweep()

        Embedding = self.env["orc.embedding"]
        self.assertEqual(
            Embedding.search_count([
                ("model", "=", "knowledge.article"),
                ("res_id", "=", article.id),
            ]),
            1,
            "precondition: the sweep should have created one embedding row",
        )

        # The delete and its index cleanup must not raise for the
        # non-admin owner of the article.
        article.with_user(self.user).unlink()

        self.assertEqual(
            Embedding.search_count([
                ("model", "=", "knowledge.article"),
                ("res_id", "=", article.id),
            ]),
            0,
            "unlink() must drop the embedding row",
        )
        self.assertEqual(
            self.Queue.search_count([
                ("model", "=", "knowledge.article"),
                ("res_id", "=", article.id),
            ]),
            0,
            "unlink() must drop any pending queue marker",
        )
