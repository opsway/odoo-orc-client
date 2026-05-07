"""Hash-skip pins the cost-saver: if the extracted text didn't
actually change, don't re-embed.

Tested at the cron-worker level — the worker is the place that
decides to skip. Tests use a mocked provider so we can assert on
``embed`` call counts."""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class HashSkipTests(TransactionCase):
    def setUp(self):
        super().setUp()
        # Set provider creds so the cron actually runs.
        self.config = self.env["orc.embedding.config"].search(
            [("is_global", "=", True)], limit=1,
        )
        self.config.write({"provider_api_key": "sk-test"})

        self.article = self.env["knowledge.article"].create({
            "name": "Hash skip test article",
            "body": "<p>same text</p>",
        })

    def _stub_provider(self, dim=4):
        """Build a provider that returns a fixed unit vector and
        records its call count."""
        mock = MagicMock()
        # Always returns one normalised vector per input.
        mock.embed.return_value = [[1.0] + [0.0] * (dim - 1)]
        mock.provider_tag.return_value = "openai:text-embedding-3-small"
        mock.dim = dim
        return mock

    def test_unchanged_body_is_not_re_embedded(self):
        Embedding = self.env["orc.embedding"]

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            Embedding._cron_reindex_sweep()
            first_call_count = provider.embed.call_count

            # No changes — re-running the sweep should hit the hash
            # check and skip the provider call.
            Embedding._cron_reindex_sweep()
            self.assertEqual(provider.embed.call_count, first_call_count)

    def test_changed_body_triggers_re_embed(self):
        Embedding = self.env["orc.embedding"]

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            Embedding._cron_reindex_sweep()
            initial_calls = provider.embed.call_count

            # Edit the body — write hook enqueues — sweep re-embeds.
            self.article.body = "<p>different text now</p>"
            Embedding._cron_reindex_sweep()
            self.assertGreater(provider.embed.call_count, initial_calls)

    def test_metadata_only_write_does_not_re_embed(self):
        # If the indexed text fields didn't change (e.g. setting a
        # tag, ticking a flag), we should NOT re-embed. The write
        # hook checks the diff before enqueueing.
        Embedding = self.env["orc.embedding"]

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            Embedding._cron_reindex_sweep()
            initial_calls = provider.embed.call_count

            # Touch a non-indexed field. Whether the queue marker
            # is created at all is up to the write hook; either way
            # the cron must not call embed.
            self.article.write({"name": "Renamed but same body"})
            Embedding._cron_reindex_sweep()
            self.assertEqual(provider.embed.call_count, initial_calls)
