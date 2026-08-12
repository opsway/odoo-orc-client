"""Index scope — which records may be sent to the provider.

README "Index scope — what gets sent to the provider" is the
contract. The assertions that matter most are the negative ones:
an out-of-scope record must not reach ``provider.embed`` at all,
and a record that falls out of scope must lose the vector it
already had.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class IndexScopeTests(TransactionCase):
    def setUp(self):
        super().setUp()
        Config = self.env["orc.embedding.config"]
        self.global_cfg = Config.search([("is_global", "=", True)], limit=1)
        self.global_cfg.write({"provider_api_key": "sk-test"})
        self.cfg = Config.search([
            ("is_global", "=", False),
            ("model_name", "=", "knowledge.article"),
        ], limit=1)
        self.Article = self.env["knowledge.article"]
        self.Embedding = self.env["orc.embedding"]
        self.Queue = self.env["orc.embedding.queue"]

    # ------------------------------------------------------- helpers

    def _stub_provider(self, dim=4):
        mock = MagicMock()
        mock.embed.return_value = [[1.0] + [0.0] * (dim - 1)]
        mock.provider_tag.return_value = "openai:text-embedding-3-small"
        mock.dim = dim
        return mock

    def _sweep(self, provider=None):
        provider = provider or self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            self.Embedding._cron_reindex_sweep()
        return provider

    def _embedding_of(self, article):
        return self.Embedding.search([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ], limit=1)

    def _queued(self, article):
        return self.Queue.search_count([
            ("model", "=", "knowledge.article"), ("res_id", "=", article.id),
        ])

    # ------------------------------------------- the per-record flag

    def test_create_with_exclude_flag_never_enqueues(self):
        article = self.Article.create({
            "name": "Secret", "body": "<p>x</p>",
            "orc_ai_index_exclude": True,
        })
        self.assertEqual(self._queued(article), 0)

    def test_setting_the_flag_enqueues_and_the_sweep_deletes_the_vector(self):
        # The flag is a writer. Flipping it must enqueue even though
        # the body did not change — otherwise the vector survives
        # until the next unrelated edit, which is indistinguishable
        # from the control not working.
        article = self.Article.create({"name": "A", "body": "<p>hello</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article), "precondition: indexed")

        article.write({"orc_ai_index_exclude": True})
        self.assertEqual(
            self._queued(article), 1,
            "flipping orc_ai_index_exclude must enqueue the record",
        )

        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertFalse(
            self._embedding_of(article),
            "an excluded record must lose the vector it already had",
        )
        self.assertEqual(self._queued(article), 0)

    def test_clearing_the_flag_reindexes(self):
        article = self.Article.create({
            "name": "A", "body": "<p>hello</p>",
            "orc_ai_index_exclude": True,
        })
        self._sweep()
        self.assertFalse(self._embedding_of(article))

        article.write({"orc_ai_index_exclude": False})
        self.assertEqual(self._queued(article), 1)
        self._sweep()
        self.assertTrue(self._embedding_of(article))

    # ---------------------------------------------------- the domain

    def test_domain_excluded_record_is_not_enqueued(self):
        self.cfg.write({"index_domain": '[("name", "!=", "Skip me")]'})
        article = self.Article.create({"name": "Skip me", "body": "<p>x</p>"})
        self.assertEqual(self._queued(article), 0)

    def test_domain_tightened_after_enqueue_drops_the_pending_row(self):
        # The sweep is the authoritative gate: a queue row can
        # outlive the settings that created it.
        article = self.Article.create({"name": "Later excluded", "body": "<p>x</p>"})
        self.assertEqual(self._queued(article), 1)

        self.cfg.write({"index_domain": '[("name", "!=", "Later excluded")]'})

        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertEqual(self._queued(article), 0)
        self.assertFalse(self._embedding_of(article))

    def test_saving_a_narrowed_domain_purges_immediately(self):
        # Saving the narrowed domain is the whole action. The cron
        # walks queue rows, and a record that silently fell out of
        # scope has no queue row — so waiting for the cron would mean
        # waiting forever.
        #
        # An earlier version of this test re-enqueued by hand
        # (`article.write({"body": ...})`) before sweeping, and passed
        # against code that never purged on save at all. Do not
        # reintroduce a nudge here: the nudge was the bug.
        article = self.Article.create({"name": "Purge me", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self.cfg.write({"index_domain": '[("name", "!=", "Purge me")]'})

        self.assertFalse(
            self._embedding_of(article),
            "narrowing the domain must delete out-of-scope vectors on save",
        )

    def test_disabling_the_model_row_purges_its_vectors(self):
        article = self.Article.create({"name": "A", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self.cfg.write({"enabled": False})
        self.assertFalse(self._embedding_of(article))

    def test_saving_a_widened_domain_does_not_enqueue(self):
        # Widening costs money. It must stay behind an explicit,
        # confirmed action rather than happening on Save.
        self.cfg.write({"index_domain": '[("name", "=", "Only this")]'})
        other = self.Article.create({"name": "Other", "body": "<p>x</p>"})
        self.assertEqual(self._queued(other), 0)

        self.cfg.write({"index_domain": False})
        self.assertEqual(
            self._queued(other), 0,
            "saving a wider domain must not enqueue a corpus by itself",
        )
        # ...but the explicit action does.
        self.cfg._sync_index_scope()
        self.assertEqual(self._queued(other), 1)

    def test_a_write_to_a_domain_field_reevaluates_scope(self):
        # The documented example domain. Moving an article into the
        # excluded category must take its vector with it, even though
        # neither the body nor the exclusion flag changed.
        self.cfg.write({"index_domain": '[("category", "!=", "private")]'})
        article = self.Article.create({
            "name": "Moves out", "body": "<p>x</p>", "category": "workspace",
        })
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        article.write({"category": "private"})
        self.assertEqual(
            self._queued(article), 1,
            "a write to a domain field must enqueue for re-evaluation",
        )

        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertFalse(self._embedding_of(article))

    def test_a_write_to_an_unrelated_field_still_does_not_enqueue(self):
        # The precision half of the previous test: the watched set is
        # derived from the domain, so a field no domain mentions must
        # not churn the queue.
        self.cfg.write({"index_domain": '[("category", "!=", "private")]'})
        article = self.Article.create({"name": "Stable", "body": "<p>x</p>"})
        self._sweep()
        self.Queue.search([]).unlink()

        article.write({"name": "Renamed"})
        self.assertEqual(self._queued(article), 0)

    def test_domain_fields_extraction_ignores_operators(self):
        self.cfg.write({
            "index_domain":
                '["|", ("category", "!=", "private"), '
                '("parent_id.category", "=", "workspace")]',
        })
        self.assertEqual(
            self.cfg._index_domain_fields(), {"category", "parent_id"},
        )

    def test_disabled_model_row_indexes_nothing(self):
        self.cfg.write({"enabled": False})
        article = self.Article.create({"name": "A", "body": "<p>x</p>"})
        self.assertEqual(self._queued(article), 0)

    # ------------------------------------------ domain validation

    def test_unparseable_domain_is_rejected_at_save(self):
        with self.assertRaises(ValidationError):
            self.cfg.write({"index_domain": "[(this is not python"})

    def test_non_list_domain_is_rejected_at_save(self):
        with self.assertRaises(ValidationError):
            self.cfg.write({"index_domain": '"name"'})

    def test_domain_naming_an_unknown_field_is_rejected_at_save(self):
        with self.assertRaises(ValidationError):
            self.cfg.write({"index_domain": '[("no_such_field", "=", 1)]'})

    def test_empty_domain_means_every_record(self):
        self.cfg.write({"index_domain": False})
        article = self.Article.create({"name": "A", "body": "<p>x</p>"})
        self.assertEqual(self._queued(article), 1)

    # ------------------------------------------------ reindex all

    def test_reindex_all_respects_scope(self):
        # The button that can send a whole corpus in one sweep. It
        # must not be the way a filtered corpus gets sent in full.
        keep = self.Article.create({"name": "Keep", "body": "<p>x</p>"})
        drop = self.Article.create({"name": "Drop", "body": "<p>x</p>"})
        flagged = self.Article.create({
            "name": "Flagged", "body": "<p>x</p>",
            "orc_ai_index_exclude": True,
        })
        self.cfg.write({"index_domain": '[("name", "!=", "Drop")]'})

        self.global_cfg.action_reindex_all()

        self.assertEqual(self._queued(keep), 1)
        self.assertEqual(self._queued(drop), 0)
        self.assertEqual(self._queued(flagged), 0)

    # --------------------------------------------------- preview

    def test_preview_counts_without_calling_the_provider(self):
        indexed = self.Article.create({"name": "Indexed", "body": "<p>x</p>"})
        self._sweep()
        pending = self.Article.create({"name": "Pending", "body": "<p>x</p>"})
        excluded = self.Article.create({
            "name": "Excluded", "body": "<p>x</p>",
            "orc_ai_index_exclude": True,
        })

        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            report = self.cfg._index_scope_report()
        provider.embed.assert_not_called()

        self.assertGreaterEqual(report["total"], 3)
        self.assertIn(excluded.id, report["excluded_flag_ids"])
        self.assertIn(pending.id, report["would_send_ids"])
        self.assertNotIn(indexed.id, report["would_send_ids"])
        self.assertNotIn(excluded.id, report["would_send_ids"])

    def test_preview_reports_vectors_that_would_be_purged(self):
        # Build the out-of-scope-with-a-vector state via the per-record
        # flag, NOT via a domain edit: saving a domain purges on the
        # spot, so a domain edit would leave nothing to report and the
        # assertion would be vacuous rather than wrong.
        article = self.Article.create({"name": "Purgeable", "body": "<p>x</p>"})
        self._sweep()
        article.write({"orc_ai_index_exclude": True})

        report = self.cfg._index_scope_report()
        self.assertIn(article.id, report["would_purge_ids"])
        self.assertIn(article.id, report["out_of_scope_ids"])

    def test_preview_action_returns_a_notification(self):
        result = self.cfg.action_preview_scope()
        self.assertEqual(result.get("tag"), "display_notification")

    # ------------------------------------------------------- sync

    def test_sync_purges_and_enqueues_then_is_idempotent(self):
        # Same reason as the preview test: the out-of-scope state comes
        # from the flag, because a domain edit already purges on save.
        stays = self.Article.create({"name": "Stays", "body": "<p>x</p>"})
        goes = self.Article.create({"name": "Goes", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(stays))
        self.assertTrue(self._embedding_of(goes))

        goes.write({"orc_ai_index_exclude": True})
        first = self.cfg._sync_index_scope()

        self.assertEqual(first["purged"], 1)
        self.assertEqual(
            first["dequeued"], 1,
            "the marker the flag write created must go with the vector",
        )
        self.assertFalse(self._embedding_of(goes))
        self.assertTrue(self._embedding_of(stays))

        second = self.cfg._sync_index_scope()
        self.assertEqual(
            second, {"purged": 0, "dequeued": 0, "enqueued": 0},
        )

    def test_narrowing_drops_pending_markers_so_widening_cannot_replay(self):
        # A pending marker on a record that has fallen out of scope is
        # deferred work. Leaving it there means widening the domain
        # later sends that record with no explicit action — the exact
        # thing the narrow/widen asymmetry exists to prevent.
        article = self.Article.create({"name": "Pending", "body": "<p>x</p>"})
        self.assertEqual(self._queued(article), 1)
        self.assertFalse(self._embedding_of(article))

        self.cfg.write({"index_domain": '[("name", "!=", "Pending")]'})
        self.assertEqual(
            self._queued(article), 0,
            "narrowing must drop pending markers, not just vectors",
        )

        self.cfg.write({"index_domain": False})
        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertFalse(self._embedding_of(article))

    def test_sync_enqueues_in_scope_records_with_no_vector(self):
        article = self.Article.create({"name": "Fresh", "body": "<p>x</p>"})
        self.Queue.search([]).unlink()
        self.assertEqual(self._queued(article), 0)

        result = self.cfg._sync_index_scope()
        self.assertGreaterEqual(result["enqueued"], 1)
        self.assertEqual(self._queued(article), 1)


    def test_sync_does_not_call_the_provider(self):
        # Cost lands on the cron, not on the button — so an operator
        # exploring the settings page cannot spend money by accident.
        self.Article.create({"name": "A", "body": "<p>x</p>"})
        provider = self._stub_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.models.orc_embedding."
            "OrcEmbedding._build_provider",
            return_value=provider,
        ):
            self.cfg._sync_index_scope()
        provider.embed.assert_not_called()

    def test_archiving_does_not_silently_purge(self):
        # The domain is evaluated with active_test=False. Otherwise
        # `search` drops archived records the domain never mentioned,
        # and archiving an article would delete its vector as a side
        # effect — which README says it must not.
        article = self.Article.create({"name": "Archived", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        # The domain reads `name`, so `active` is not a watched field
        # and archiving enqueues nothing on its own. Enqueue by hand:
        # what's under test is the predicate, not the trigger.
        self.cfg.write({"index_domain": '[("name", "!=", "Something else")]'})
        article.write({"active": False})
        self.Queue.create({"model": "knowledge.article", "res_id": article.id})
        self._sweep()

        self.assertTrue(
            self._embedding_of(article),
            "archiving must not be an implicit exclusion",
        )

    def test_active_in_the_domain_does_purge_on_archive(self):
        article = self.Article.create({"name": "Opt in", "body": "<p>x</p>"})
        self._sweep()
        self.cfg.write({"index_domain": '[("active", "=", True)]'})

        # Here `active` IS a watched field, so archiving enqueues by
        # itself — no manual nudge (which would also collide with the
        # queue's UNIQUE(model, res_id)).
        article.write({"active": False})
        self.assertEqual(self._queued(article), 1)
        self._sweep()

        self.assertFalse(
            self._embedding_of(article),
            "an operator who asks for it must get archive-purges",
        )

    # ------------------------- an unevaluable domain touches nothing

    def _break_the_domain(self):
        """Store a domain that validates now but not later.

        Mirrors the real failure: the domain named a field that a
        module upgrade renamed or dropped. The constraint can't help,
        because the domain was legal when it was saved.
        """
        self.cfg.write({"index_domain": '[("name", "!=", "x")]'})
        self.env.cr.execute(
            "UPDATE orc_embedding_config SET index_domain = %s WHERE id = %s",
            ['[("gone_in_an_upgrade", "=", 1)]', self.cfg.id],
        )
        self.cfg.invalidate_recordset(["index_domain"])

    def test_a_broken_domain_does_not_purge(self):
        # "We can't tell what's in scope" must not mean "delete the
        # corpus". A transient-looking config break that wiped every
        # vector would cost a full re-embed to recover.
        article = self.Article.create({"name": "Keep me", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self._break_the_domain()
        provider = self._sweep()

        provider.embed.assert_not_called()
        self.assertTrue(
            self._embedding_of(article),
            "a domain that cannot be evaluated must not delete vectors",
        )

    def test_a_broken_domain_does_not_send(self):
        self._break_the_domain()
        self.Article.create({"name": "New", "body": "<p>x</p>"})
        provider = self._sweep()
        provider.embed.assert_not_called()

    def test_a_broken_domain_does_not_block_saving_articles(self):
        # The predicate runs inside the author's transaction. A raise
        # would roll back their save, so a bad index setting would
        # make articles unsaveable.
        self._break_the_domain()
        article = self.Article.create({"name": "Still saveable", "body": "<p>x</p>"})
        self.assertTrue(article.exists())
        article.write({"body": "<p>edited</p>"})
        self.assertIn("edited", article.body)

    def test_a_broken_domain_refuses_sync_and_preview(self):
        self._break_the_domain()
        self.assertEqual(
            self.cfg._sync_index_scope(),
            {"purged": 0, "dequeued": 0, "enqueued": 0},
        )
        with self.assertRaises(UserError):
            self.cfg.action_preview_scope()

    def test_a_broken_domain_refuses_reindex_all_before_wiping(self):
        # Reindex all deletes the index and then re-enqueues what is
        # in scope. With scope unknown the predicate returns nothing,
        # so the delete would stand alone and the corpus would be gone
        # — at the cost of a full re-embed to recover.
        article = self.Article.create({"name": "Precious", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self._break_the_domain()
        with self.assertRaises(UserError):
            self.global_cfg.action_reindex_all()

        self.assertTrue(
            self._embedding_of(article),
            "the refusal must happen before anything is deleted",
        )

    def test_disabling_purges_even_when_the_domain_is_broken(self):
        # Disabled is a definitive empty scope, not an unknown one, so
        # the domain-health guard must not suppress this purge. If it
        # did, re-enabling the row later would put the stale vectors
        # straight back into semantic_search, which gates on `enabled`
        # rather than on scope.
        article = self.Article.create({"name": "A", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self._break_the_domain()
        self.cfg.write({"enabled": False})

        self.assertFalse(
            self._embedding_of(article),
            "disabling must purge regardless of domain health",
        )

    # --------------------------------- preview counts pending edits

    def test_preview_counts_a_pending_re_embed(self):
        # An edited article keeps its vector and gains a marker. The
        # next sweep re-sends its text, so the preview must count it —
        # otherwise the figure understates transmission in exactly the
        # case an operator consults it for.
        article = self.Article.create({"name": "Edited", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        article.write({"body": "<p>substantially different</p>"})
        report = self.cfg._index_scope_report()

        self.assertIn(article.id, report["would_send_ids"])
        self.assertGreaterEqual(report["queued"], 1)

    # -------------------------------------- deleting the config row

    def test_deleting_the_config_row_purges_the_model(self):
        # No config row means an empty scope. Leaving the vectors
        # orphaned means nothing ever revisits them, and re-creating
        # the row makes them searchable again on save — even under a
        # narrower domain, since semantic_search gates on `enabled`.
        article = self.Article.create({"name": "A", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self.cfg.unlink()

        self.assertFalse(
            self._embedding_of(article),
            "deleting the config row must take its vectors with it",
        )
        self.assertEqual(self._queued(article), 0)

    def test_deleting_the_global_row_purges_nothing(self):
        # The global row holds provider credentials, not a scope. It
        # must not be a corpus-delete button.
        article = self.Article.create({"name": "A", "body": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self._embedding_of(article))

        self.global_cfg.unlink()
        self.assertTrue(self._embedding_of(article))

    # ------------------------------ predicate on a model with no flag

    def test_model_without_the_exclude_field_excludes_nothing(self):
        # Adding a model must stay a config-row change: a model that
        # doesn't carry orc_ai_index_exclude is "nothing excluded",
        # not "everything excluded".
        cfg = self.env["orc.embedding.config"].create({
            "is_global": False,
            "model_name": "res.partner",
            "enabled": True,
            "text_field_path": "comment",
            "text_extractor": "plain",
        })
        partner = self.env["res.partner"].create({
            "name": "Scope probe", "comment": "hello",
        })
        self.assertNotIn("orc_ai_index_exclude", partner._fields)
        self.assertEqual(cfg._filter_indexable(partner), partner)
