"""Daily token cap enforcement.

Until 15.0.0.3.0 ``daily_token_cap`` was declared on the config
model, rendered in the Settings form, described in README as
"cron pauses on overrun" and recommended by AGENTS.md as the way
to pause indexing — and read by no code at all. These tests pin
the behaviour those three documents already promised.

The 0-means-pause direction is the one to keep an eye on: the
natural ``if cap and spent > cap`` spelling silently turns the
documented pause switch into "unlimited".
"""
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import tagged

from odoo.addons.orc_client_semantic_search.providers.base import (
    EmbeddingProviderError,
)

from .common import SweepCase


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class DailyTokenCapTests(SweepCase):
    def setUp(self):
        super().setUp()
        Config = self.env["orc.embedding.config"]
        self.global_cfg = Config.search([("is_global", "=", True)], limit=1)
        self.global_cfg.write({
            "provider_api_key": "sk-test",
            "daily_token_cap": 1_000_000,
            "tokens_used_today": 0,
            "tokens_usage_date": fields.Date.context_today(self.global_cfg),
        })
        self.Article = self.env["document.page"]
        self.Embedding = self.env["orc.embedding"]
        self.Queue = self.env["orc.embedding.queue"]

    def _used(self):
        """Today's total, read the way the cron reads it.

        Not `self.global_cfg.tokens_used_today`: the counter is written
        on an independent cursor, so reading it back through the ORM
        asserts on whatever the test transaction's snapshot happens to
        show rather than on the value the cron would act upon.
        """
        used, _date = self.global_cfg._token_budget_state()
        return used

    def _stub_provider(self, dim=4):
        mock = MagicMock()
        mock.embed.return_value = [[1.0] + [0.0] * (dim - 1)]
        mock.provider_tag.return_value = "openai:text-embedding-3-small"
        mock.dim = dim
        return mock

    def _usage_provider(self, tokens, dim=4, raises=None, returns=None):
        """A provider double that reports usage on EVERY call.

        The real provider assigns ``last_usage_tokens`` per call and
        the sweep clears it after charging, so a mock carrying one
        static value is only charged for the first record and silently
        falls back to the chars÷4 estimate for the rest — which makes
        an accumulation test measure the estimate instead of the
        thing under test.
        """
        mock = MagicMock()
        mock.dim = dim
        mock.provider_tag.return_value = "openai:text-embedding-3-small"
        vector = returns if returns is not None else [[1.0] + [0.0] * (dim - 1)]

        def _embed(_texts):
            mock.last_usage_tokens = tokens
            if raises is not None:
                raise raises
            return vector

        mock.embed.side_effect = _embed
        mock.last_usage_tokens = None
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

    # --------------------------------------------------- accounting

    def test_a_successful_embed_adds_to_todays_total(self):
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})
        self._sweep()
        self.assertGreater(
            self._used(), 0,
            "the sweep must record what it spent",
        )
        self.assertEqual(
            self.global_cfg.tokens_usage_date,
            fields.Date.context_today(self.global_cfg),
        )

    def test_a_hash_skip_costs_nothing(self):
        article = self.Article.create({"name": "A", "content": "<p>hello</p>"})
        self._sweep()
        spent_after_first = self._used()

        # Re-enqueue without changing the content: hash-skip path, no
        # provider call, so no tokens.
        self.Queue.create({"model": "document.page", "res_id": article.id})
        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertEqual(self._used(), spent_after_first)

    def test_a_stale_day_resets_the_counter(self):
        self.global_cfg.write({
            "tokens_used_today": 999_999_999,
            "tokens_usage_date": "2020-01-01",
        })
        self.Article.create({"name": "A", "content": "<p>hello</p>"})
        provider = self._sweep()

        provider.embed.assert_called()
        self.assertLess(
            self._used(), 999_999_999,
            "yesterday's spend must not bind today",
        )
        self.assertEqual(
            self.global_cfg.tokens_usage_date,
            fields.Date.context_today(self.global_cfg),
        )

    # ----------------------------------------------- enforcement

    def test_cap_zero_pauses_the_sweep(self):
        # AGENTS.md documents this as the pause switch. It must not
        # read as "no limit".
        self.global_cfg.write({"daily_token_cap": 0})
        article = self.Article.create({"name": "A", "content": "<p>hello</p>"})

        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertEqual(
            self.Queue.search_count([
                ("model", "=", "document.page"), ("res_id", "=", article.id),
            ]),
            1,
            "a paused sweep must leave the work queued, not discard it",
        )

    def test_exhausted_cap_stops_the_sweep(self):
        self.global_cfg.write({
            "daily_token_cap": 10,
            "tokens_used_today": 10,
            "tokens_usage_date": fields.Date.context_today(self.global_cfg),
        })
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})

        provider = self._sweep()
        provider.embed.assert_not_called()

    def test_the_sweep_stops_mid_pass_once_the_cap_is_crossed(self):
        # Enough articles that the first embed exhausts a tiny cap.
        # The pass must stop rather than run to the end of the queue.
        for i in range(5):
            self.Article.create({
                "name": "Article %s" % i,
                "content": "<p>" + ("word " * 200) + "</p>",
            })
        self.global_cfg.write({
            "daily_token_cap": 1,
            "tokens_used_today": 0,
            "tokens_usage_date": fields.Date.context_today(self.global_cfg),
        })

        provider = self._sweep()
        self.assertLessEqual(
            provider.embed.call_count, 1,
            "the cap must bind within a pass, not only between passes",
        )
        self.assertGreater(
            self.Queue.search_count([("model", "=", "document.page")]), 0,
            "unspent work stays queued for tomorrow",
        )

    # ------------------------------- failures that were still billed

    def test_a_billed_validation_failure_is_charged(self):
        # The dangerous shape: the HTTP call succeeds and is billed,
        # then our own dimension check rejects the response. Skipping
        # the charge there means a wrong `vector_dim` bills on every
        # cron pass forever — there is no attempt ceiling — while
        # tokens_used_today sits at zero and the cap never engages.
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})

        provider = self._usage_provider(
            4242,
            raises=EmbeddingProviderError(
                "dimension mismatch: expected 4, got 1536",
            ),
        )
        self._sweep(provider)

        self.assertEqual(
            self._used(), 4242,
            "a failure the upstream billed must still be charged",
        )

    def test_a_mis_shaped_vector_is_charged(self):
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})

        # Wrong-length vector: HTTP fine, our own shape check rejects.
        provider = self._usage_provider(11, returns=[[1.0, 0.0]])
        self._sweep(provider)
        self.assertEqual(self._used(), 11)

    def test_an_unbilled_failure_is_not_charged(self):
        # The other direction: a network error costs nothing, so it
        # must not eat the day's budget. The provider reports None.
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})

        provider = MagicMock()
        provider.dim = 4
        provider.provider_tag.return_value = "openai:text-embedding-3-small"
        provider.last_usage_tokens = None
        provider.embed.side_effect = EmbeddingProviderError(
            "network error contacting https://api.openai.com/v1/embeddings",
        )

        self._sweep(provider)
        self.assertEqual(self._used(), 0)

    def test_repeated_billed_failures_eventually_exhaust_the_cap(self):
        # The property the fix exists for: a misconfiguration cannot
        # bill without limit. Three passes at 40 tokens against a cap
        # of 100 must stop the fourth.
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})
        self.global_cfg.write({"daily_token_cap": 100})

        calls = []
        for _ in range(4):
            p = self._usage_provider(
                40, raises=EmbeddingProviderError("dimension mismatch"),
            )
            self._sweep(p)
            calls.append(p.embed.call_count)

        self.assertEqual(calls[:3], [1, 1, 1])
        self.assertEqual(
            calls[3], 0,
            "once the cap is spent, a failing row must stop being re-billed",
        )

    def test_charges_accumulate_within_one_sweep(self):
        # The counter is written on an independent cursor, so the
        # sweep's own REPEATABLE READ snapshot never sees those
        # commits. A read-modify-write off the recordset would compute
        # the same `original + this_request` every time and record a
        # hundred charges as one, which is a cap that does not cap.
        for i in range(4):
            self.Article.create({
                "name": "Article %s" % i, "content": "<p>hello world %s</p>" % i,
            })

        provider = self._usage_provider(100)
        self._sweep(provider)

        self.assertEqual(provider.embed.call_count, 4)
        self.assertEqual(
            self._used(), 400,
            "four charges of 100 must total 400, not 100",
        )

    def test_the_cap_binds_across_passes(self):
        # The end-to-end property: accumulation plus enforcement. A cap
        # of 250 at 100 tokens a call allows two calls, then stops.
        for i in range(5):
            self.Article.create({
                "name": "Article %s" % i, "content": "<p>hello world %s</p>" % i,
            })
        self.global_cfg.write({"daily_token_cap": 250})

        total_calls = 0
        for _ in range(3):
            provider = self._usage_provider(100)
            self._sweep(provider)
            total_calls += provider.embed.call_count

        self.assertEqual(
            total_calls, 3,
            "250 tokens at 100 a call is three calls (the third crosses), "
            "never five",
        )

    def test_the_charge_survives_a_rollback_of_the_sweep(self):
        # The counter is written on its own cursor, because a provider
        # charge cannot be rolled back. If the pass unwinds after a
        # call was billed, the accounting must not unwind with it —
        # otherwise the next pass re-sends the same records with the
        # cap still reading zero, which is exactly what the cap exists
        # to prevent.
        #
        # Honest limit, and it is a real one: this cannot assert
        # durability after the fact from a TransactionCase. In test
        # mode (entered in setUp) `pool.cursor()` proxies the test
        # transaction, so the charge unwinds with the savepoint
        # `assertRaises` opens. Outside test mode it would be a genuine
        # second connection — which then blocks on the row lock this
        # transaction already holds on the config row, and the test
        # hangs instead of failing.
        #
        # So the assertion moves to the moment that IS observable: the
        # charge must already be recorded when the later failure
        # happens, and recorded through the independent path rather
        # than as part of the sweep's own writes. Whether that survives
        # a real rollback follows from the cursor being independent,
        # which is asserted separately below. Cross-transaction
        # durability rests on the code shape, reviewed not executed.
        self.Article.create({"name": "A", "content": "<p>hello world</p>"})

        charged_at_failure = []

        def blow_up(*args, **kwargs):
            charged_at_failure.append(self._used())
            raise RuntimeError("something later blew up")

        provider = self._stub_provider()
        with self.assertRaises(RuntimeError):
            with patch(
                "odoo.addons.orc_client_semantic_search.models.orc_embedding."
                "OrcEmbedding._build_provider",
                return_value=provider,
            ):
                with patch.object(
                    type(self.Embedding), "create", side_effect=blow_up,
                ):
                    self.Embedding._cron_reindex_sweep()

        provider.embed.assert_called()
        self.assertTrue(charged_at_failure, "the failure path never ran")
        self.assertGreater(
            charged_at_failure[0], 0,
            "a billed call must be charged before the pass can unwind, "
            "not as part of the transaction that unwinds",
        )

    def test_the_charge_is_written_on_a_cursor_of_its_own(self):
        # The other half of the guarantee above: the counter must not
        # ride on the sweep's transaction. Asserted on the shape, since
        # the durability it buys cannot be observed from here.
        cursors = []
        real_cursor = type(self.registry).cursor

        def spy(registry_self, *args, **kwargs):
            cursors.append(True)
            return real_cursor(registry_self, *args, **kwargs)

        with patch.object(type(self.registry), "cursor", spy):
            self.global_cfg._token_budget_consume(42)

        self.assertTrue(
            cursors,
            "the charge must go through registry.cursor(), not self.env.cr — "
            "a provider charge cannot be rolled back, so its record must not "
            "be rollback-able either",
        )
        self.assertEqual(self._used(), 42)

    def test_an_unreadable_text_field_does_not_abort_the_pass(self):
        # A removed text_field_path used to raise straight out of the
        # sweep, taking every other queued row with it.
        cfg = self.env["orc.embedding.config"].search([
            ("is_global", "=", False),
            ("model_name", "=", "document.page"),
        ], limit=1)
        self.Article.create({"name": "A", "content": "<p>hello</p>"})
        cfg.write({"text_field_path": "gone_in_an_upgrade"})

        provider = self._stub_provider()
        self._sweep(provider)  # must not raise

        provider.embed.assert_not_called()
        row = self.Queue.search([("model", "=", "document.page")], limit=1)
        self.assertTrue(row, "the row stays queued for after the fix")
        self.assertEqual(row.attempts, 1)
        self.assertIn("gone_in_an_upgrade", row.last_error or "")

    def test_a_paused_sweep_still_drops_out_of_scope_rows(self):
        # Bookkeeping that costs nothing must not be held hostage to
        # the budget: an excluded record's vector should go even when
        # the cap is spent, or exclusion silently waits for a refill.
        article = self.Article.create({"name": "Purge me", "content": "<p>x</p>"})
        self._sweep()
        self.assertTrue(self.Embedding.search([
            ("model", "=", "document.page"), ("res_id", "=", article.id),
        ]))

        article.write({"orc_ai_index_exclude": True})
        self.global_cfg.write({"daily_token_cap": 0})

        provider = self._sweep()
        provider.embed.assert_not_called()
        self.assertFalse(
            self.Embedding.search([
                ("model", "=", "document.page"), ("res_id", "=", article.id),
            ]),
            "purging is free; a spent budget must not keep an excluded "
            "record's vector alive",
        )
