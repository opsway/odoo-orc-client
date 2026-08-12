import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval

from ..providers.openai import OpenAIEmbeddingProvider


_logger = logging.getLogger(__name__)


# The per-record opt-out. Named once here and read via ``_fields``
# so a model that doesn't carry it means "nothing excluded" rather
# than forcing every future indexed model to inherit a mixin.
EXCLUDE_FIELD = "orc_ai_index_exclude"


# Two purposes in one model:
#   - Singleton row (``is_global=True``) holds provider creds + cron
#     settings. Exactly one such row may exist; created by demo data.
#   - Per-model rows (``is_global=False``) hold the indexable-model
#     toggle and field selection. One row per model_name.
#
# The dual-purpose shape keeps the surface small (one menu, one
# search view) and makes the Settings UI render the singleton's
# provider fields in a header section above the per-model list. The
# alternative — two separate models — felt like overkill for v1.
class OrcEmbeddingConfig(models.Model):
    _name = "orc.embedding.config"
    _description = "AI Workplace semantic search — provider config + per-model toggles"
    _rec_name = "model_name"

    is_global = fields.Boolean(
        string="Global config row",
        default=False,
        index=True,
    )

    # ------------------------------- global-row fields (provider)

    provider_kind = fields.Selection(
        selection=[
            ("openai", "OpenAI"),
            ("voyage", "Voyage"),
            ("openai_compat", "OpenAI-compatible endpoint"),
        ],
        string="Provider kind",
    )
    provider_url = fields.Char(
        string="Endpoint URL",
        help="POST endpoint for embeddings. Defaults to OpenAI's URL.",
    )
    provider_api_key = fields.Char(
        string="API key",
        help="Stored as-is; viewable only by the technical group.",
    )
    provider_model = fields.Char(
        string="Model",
        help="e.g. text-embedding-3-small",
    )
    vector_dim = fields.Integer(
        string="Vector dimensions",
        help="Must match the chosen model. Validated by the 'Test provider' button.",
    )
    cron_interval_minutes = fields.Integer(
        string="Cron interval (minutes)",
        default=5,
    )
    daily_token_cap = fields.Integer(
        string="Daily token cap",
        default=1_000_000,
        help="Tokens the reindex cron may spend per calendar day. It "
             "stops for the rest of the day on overrun and resumes at "
             "midnight. Set to 0 to pause indexing entirely.",
    )
    tokens_used_today = fields.Integer(
        string="Tokens used today",
        readonly=True,
        help="Running total for the day in 'Tokens usage date'. Reset by "
             "the cron on its first pass of a new day.",
    )
    tokens_usage_date = fields.Date(
        string="Tokens usage date",
        readonly=True,
        help="The day 'Tokens used today' refers to.",
    )

    # ----------------------------- per-model-row fields (indexed)

    model_name = fields.Char(
        string="Odoo model",
        help="e.g. knowledge.article",
        index=True,
    )
    enabled = fields.Boolean(
        string="Enabled",
        default=True,
    )
    index_domain = fields.Char(
        string="Index domain",
        help="Odoo domain limiting which records of this model may be "
             "indexed — i.e. which records have their text sent to the "
             "embedding provider. Empty means every record. Example: "
             '[("category", "!=", "private")].',
    )
    text_field_path = fields.Char(
        string="Text field path",
        help="Dotted path to the text source on the record. e.g. 'body'.",
    )
    text_extractor = fields.Selection(
        selection=[
            ("html_strip", "HTML — strip tags to plain text"),
            ("plain", "Plain text — use as-is"),
            ("attachment", "Attachment — extract text via pypdf etc."),
        ],
        string="Extractor",
        default="html_strip",
    )

    _sql_constraints = [
        (
            "unique_global_singleton",
            "EXCLUDE (is_global WITH =) WHERE (is_global = TRUE)",
            "Only one global config row may exist.",
        ),
        (
            "unique_per_model_name",
            "UNIQUE (model_name)",
            "Each Odoo model may have only one config row.",
        ),
    ]

    @api.constrains("is_global", "model_name", "provider_kind")
    def _check_row_kind_fields(self):
        for rec in self:
            if rec.is_global:
                if rec.model_name:
                    raise ValidationError(
                        _("Global config row must not set 'Odoo model'."),
                    )
            else:
                if not rec.model_name:
                    raise ValidationError(
                        _("Per-model config row must set 'Odoo model'."),
                    )
                if rec.provider_kind:
                    raise ValidationError(
                        _("Per-model config row must not set provider fields."),
                    )

    @api.constrains("index_domain", "model_name", "is_global")
    def _check_index_domain(self):
        """Reject a domain that doesn't parse or that the model
        refuses, at save time.

        The cron is the wrong place to discover a typo: it runs
        unattended, and a domain it can't evaluate would either
        crash the sweep or — worse, depending on how the error is
        handled — fall through to indexing everything.
        """
        for rec in self:
            if rec.is_global or not rec.index_domain:
                continue
            domain = rec._parse_index_domain(raise_on_error=True)
            target = self.env.get(rec.model_name)
            if target is None:
                # Model isn't installed here; nothing to trial
                # against. The domain still had to parse.
                continue
            try:
                target.sudo().search_count(domain)
            except Exception as exc:
                raise ValidationError(_(
                    "Index domain is not valid for %(model)s: %(err)s"
                ) % {"model": rec.model_name, "err": exc}) from exc

    def _parse_index_domain(self, raise_on_error=False):
        """Return the domain as a list of leaves, or ``[]``.

        ``raise_on_error`` is for the save-time constraint. The
        runtime callers pass False: by then the value has already
        been validated once, and a sweep is not the place to raise.
        """
        self.ensure_one()
        if not self.index_domain:
            return []
        try:
            domain = safe_eval(self.index_domain)
        except Exception as exc:
            if raise_on_error:
                raise ValidationError(_(
                    "Index domain is not a valid Python expression: %s"
                ) % exc) from exc
            _logger.error(
                "index_domain on %s does not parse (%s); treating the "
                "model as out of scope rather than indexing everything.",
                self.model_name, exc,
            )
            return None
        if not isinstance(domain, list):
            if raise_on_error:
                raise ValidationError(_(
                    "Index domain must be a list of leaves, e.g. "
                    '[("field", "=", value)] — got %s.'
                ) % type(domain).__name__)
            _logger.error(
                "index_domain on %s is a %s, not a list; treating the "
                "model as out of scope.", self.model_name,
                type(domain).__name__,
            )
            return None
        return domain

    def _index_domain_fields(self):
        """Field names the domain reads, first path segment only.

        Used by the indexed model's write hook to decide whether a
        write can have changed the record's scope. Taking only the
        first segment means a domain over ``parent_id.category``
        re-evaluates when the article is re-parented.

        Known limit: a write to the *related* record (that parent's
        own category) fires no hook on the article and is therefore
        not caught. "Sync index scope" is the answer for those; a
        general dependency graph is not something a domain string
        can give us.
        """
        self.ensure_one()
        domain = self._parse_index_domain()
        if not domain:
            return set()
        names = set()
        for leaf in domain:
            if isinstance(leaf, str):
                # '&', '|', '!'
                continue
            try:
                left = leaf[0]
            except (TypeError, IndexError):
                continue
            if isinstance(left, str) and left:
                names.add(left.split(".")[0])
        return names

    # ------------------------------------------------- index scope

    def write(self, vals):
        """Apply a narrowed scope immediately, for free.

        Changing ``index_domain`` or ``enabled`` must not leave
        already-indexed records searchable until something else
        happens to touch them: the cron walks queue rows, and a
        record that falls out of scope has no queue row. So the
        purge runs on save.

        The other direction is deliberately NOT automatic. Widening
        a domain enqueues records, and enqueued records get sent to
        the provider — that is a spend, and a spend belongs behind an
        explicit action with a confirmation ("Sync index scope" or
        "Reindex all"), not behind pressing Save on a settings page.
        """
        scope_keys = {"index_domain", "enabled"}
        touches_scope = bool(scope_keys & set(vals))
        result = super().write(vals)
        if touches_scope:
            for rec in self:
                if rec.is_global or not rec.model_name:
                    continue
                purged = rec._sync_index_scope(enqueue=False)["purged"]
                if purged:
                    _logger.info(
                        "scope narrowed on %s: deleted %d vector(s) that are "
                        "no longer in scope.", rec.model_name, purged,
                    )
        return result

    def unlink(self):
        """Deleting a per-model row takes that model's index with it.

        Without this, deleting the row orphans its vectors: nothing
        else ever revisits them, and re-creating the row — especially
        with a narrower domain — makes stale, out-of-scope vectors
        searchable again the moment it is saved, because
        ``semantic_search`` gates on ``enabled`` rather than on scope.

        Same reasoning as disabling: no row means an empty scope, and
        an empty scope means no vectors. Domain health is irrelevant
        here, so the unknown-scope guard does not apply.
        """
        Embedding = self.env["orc.embedding"].sudo()
        Queue = self.env["orc.embedding.queue"].sudo()
        models = [
            rec.model_name for rec in self
            if not rec.is_global and rec.model_name
        ]
        if models:
            purged = len(Embedding.search([("model", "in", models)]))
            Embedding.search([("model", "in", models)]).unlink()
            Queue.search([("model", "in", models)]).unlink()
            _logger.info(
                "config row(s) for %s deleted: dropped %d vector(s) and any "
                "pending markers.", ", ".join(models), purged,
            )
        return super().unlink()

    def _filter_indexable(self, records):
        """Return the subset of ``records`` that may be indexed.

        The single decision point for "may this record's text be
        sent to the provider". Every caller — the create/write
        hooks, the cron sweep, Reindex all, the preview and the
        sync — routes through here. See AGENTS.md "One scope
        predicate" for why a second copy is not acceptable.

        Fails closed: a model row that is disabled, or whose domain
        cannot be evaluated, indexes nothing.
        """
        self.ensure_one()
        empty = records.browse()
        if not records:
            return empty
        if not self.enabled:
            return empty

        domain = self._parse_index_domain()
        if domain is None:
            return empty

        kept = records
        if domain:
            # Evaluated by the ORM rather than in memory so the
            # preview counts and the runtime gate agree leaf for
            # leaf, including dotted paths. sudo() because scope is
            # an operator decision — it must not vary by who
            # happened to trigger the write.
            # active_test=False, or `search` would drop archived
            # records that the domain never mentioned — turning
            # "archive an article" into "delete its vector" as a side
            # effect. README says archiving keeps the embedding; put
            # ("active", "=", True) in the domain if you want the
            # other behaviour.
            #
            # The catch is load-bearing, not defensive padding. The
            # domain was validated when it was saved, but a later
            # module upgrade can rename or drop the field it names.
            # This predicate runs inside the article author's own
            # transaction (create/write hook), so an escaping
            # exception would roll back their save — a broken index
            # setting would make articles unsaveable. Fail closed
            # instead: nothing is indexable, so nothing is
            # transmitted. Purging is separately suppressed while the
            # domain is broken (see ``_index_scope_error``), because
            # "we can't tell what's in scope" must not mean "delete
            # the corpus".
            try:
                allowed_ids = set(records.sudo().with_context(
                    active_test=False,
                ).search([("id", "in", records.ids)] + domain).ids)
            except Exception as exc:
                _logger.error(
                    "index_domain on %s could not be evaluated (%s); "
                    "treating every record as out of scope for this call.",
                    self.model_name, exc,
                )
                return empty
            kept = records.filtered(lambda r: r.id in allowed_ids)

        if EXCLUDE_FIELD in records._fields:
            kept = kept.filtered(lambda r: not r[EXCLUDE_FIELD])

        return kept

    def _index_scope_error(self):
        """Return a message if the stored domain can't be evaluated
        right now, else None.

        Separates "this record is out of scope" from "we cannot tell
        what is in scope". The two must not be conflated: the first
        means delete the vector, the second must mean touch nothing.
        Callers that delete — the cron's purge branch, the save-time
        purge, the sync — check this first. One query, called once
        per config per cron pass, not per record.
        """
        self.ensure_one()
        if self.is_global or not self.model_name or not self.index_domain:
            return None
        if not self.enabled:
            # A disabled row's scope is not unknown — it is empty, by
            # definition, whatever the domain says. Reporting an error
            # here would let a broken domain suppress the purge that
            # disabling is supposed to perform, and re-enabling the
            # row later would bring those stale vectors straight back
            # into semantic_search (which gates on `enabled`, not on
            # scope).
            return None
        domain = self._parse_index_domain()
        if domain is None:
            return "index_domain does not parse"
        target = self.env.get(self.model_name)
        if target is None:
            return None
        try:
            target.sudo().with_context(active_test=False).search_count(domain)
        except Exception as exc:
            return "index_domain is no longer valid for %s: %s" % (
                self.model_name, exc,
            )
        return None

    def _scope_target_model(self):
        """Return the indexed model's recordset, or None."""
        self.ensure_one()
        target = self.env.get(self.model_name)
        if target is None:
            return None
        return target.sudo().with_context(active_test=False)

    def _index_scope_report(self):
        """Count what the current scope would do. Reads only — no
        provider call, no write.

        ``would_send_ids`` is the number that matters before
        widening a domain or filling in the API key: records that
        are in scope and have no vector yet, i.e. exactly what the
        next cron passes would transmit.
        """
        self.ensure_one()
        target = self._scope_target_model()
        if target is None:
            return {
                "model_name": self.model_name,
                "installed": False,
                "total": 0,
                "in_scope": 0,
                "excluded_domain_ids": [],
                "excluded_flag_ids": [],
                "indexed": 0,
                "would_send_ids": [],
                "would_purge_ids": [],
            }

        records = target.search([])
        in_scope = self._filter_indexable(records)
        in_scope_ids = set(in_scope.ids)

        # Split the exclusions so the operator can tell "my domain
        # is too narrow" from "someone ticked the box".
        excluded_flag_ids = []
        if EXCLUDE_FIELD in records._fields:
            excluded_flag_ids = records.filtered(
                lambda r: r[EXCLUDE_FIELD]
            ).ids
        excluded_domain_ids = [
            rid for rid in records.ids
            if rid not in in_scope_ids and rid not in set(excluded_flag_ids)
        ]

        indexed_ids = set(self.env["orc.embedding"].sudo().search([
            ("model", "=", self.model_name),
        ]).mapped("res_id"))
        queued_ids = set(self.env["orc.embedding.queue"].sudo().search([
            ("model", "=", self.model_name),
        ]).mapped("res_id"))

        # "Would be sent" is in-scope records with no vector, PLUS
        # in-scope records that already have one but carry a pending
        # marker: an edited article keeps its embedding and gains a
        # marker, and the next sweep re-embeds it. Counting only the
        # first set understates transmission in exactly the case an
        # operator consults this for.
        #
        # It over-reports where a queued record's text turns out
        # unchanged (the sweep hash-skips it, no call). Over-reporting
        # a transmission figure is the safe direction, and knowing
        # would mean running every extractor and hash here.
        would_send = (in_scope_ids - indexed_ids) | (in_scope_ids & queued_ids)

        return {
            "model_name": self.model_name,
            "installed": True,
            "scope_error": self._index_scope_error(),
            "total": len(records),
            "in_scope": len(in_scope),
            "out_of_scope_ids": sorted(set(records.ids) - in_scope_ids),
            "excluded_domain_ids": excluded_domain_ids,
            "excluded_flag_ids": excluded_flag_ids,
            "indexed": len(indexed_ids),
            "queued": len(queued_ids),
            "would_send_ids": sorted(would_send),
            "would_purge_ids": sorted(indexed_ids - in_scope_ids),
        }

    def action_preview_scope(self):
        """Settings-page button. Renders ``_index_scope_report``."""
        self.ensure_one()
        if self.is_global:
            raise UserError(_("Preview scope applies to a per-model row."))

        report = self._index_scope_report()
        if not report["installed"]:
            raise UserError(_(
                "Model %s is not installed on this database."
            ) % self.model_name)
        if report["scope_error"]:
            raise UserError(_(
                "The index domain cannot be evaluated, so scope is unknown "
                "and nothing is being indexed. Fix the domain first.\n\n%s"
            ) % report["scope_error"])

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Index scope — %s") % self.model_name,
                "message": _(
                    "%(total)s records. In scope: %(in_scope)s "
                    "(%(dom)s excluded by domain, %(flag)s excluded per "
                    "record). Already indexed: %(indexed)s.\n"
                    "Would be SENT to the provider on the next passes: "
                    "%(send)s. Vectors that would be DELETED as "
                    "out-of-scope: %(purge)s."
                ) % {
                    "total": report["total"],
                    "in_scope": report["in_scope"],
                    "dom": len(report["excluded_domain_ids"]),
                    "flag": len(report["excluded_flag_ids"]),
                    "indexed": report["indexed"],
                    "send": len(report["would_send_ids"]),
                    "purge": len(report["would_purge_ids"]),
                },
                "type": "info",
                "sticky": True,
            },
        }

    def _sync_index_scope(self, enqueue=True):
        """Bring the index in line with the current scope, now.

        Deletes out-of-scope vectors and queue markers and, when
        ``enqueue`` is true, enqueues in-scope records that have
        none. Idempotent — a second call on an unchanged corpus
        returns zeroes.

        Enqueues rather than embeds: the provider spend stays on the
        cron, so an operator cannot run up a bill by pressing a
        button on the settings page.

        ``enqueue=False`` is the purge-only form used when a scope
        change is saved — see ``write``. Purging is free and safety-
        critical; enqueueing costs money and stays explicit.
        """
        self.ensure_one()
        Embedding = self.env["orc.embedding"].sudo()
        Queue = self.env["orc.embedding.queue"].sudo()

        report = self._index_scope_report()
        if not report["installed"]:
            return {"purged": 0, "dequeued": 0, "enqueued": 0}
        if report["scope_error"]:
            # Every branch below either deletes or transmits, and we
            # don't currently know what is in scope. Do neither.
            _logger.error(
                "sync_index_scope refused on %s: %s",
                self.model_name, report["scope_error"],
            )
            return {"purged": 0, "dequeued": 0, "enqueued": 0}

        purged = 0
        purge_ids = report["would_purge_ids"]
        if purge_ids:
            stale = Embedding.search([
                ("model", "=", self.model_name),
                ("res_id", "in", purge_ids),
            ])
            purged = len(stale)
            stale.unlink()

        # Queue markers go for EVERY out-of-scope record, not only the
        # ones that had a vector. A pending marker on an out-of-scope
        # record is deferred work that becomes live the moment the
        # domain widens again — which would send that record without
        # the explicit, confirmed action that widening is supposed to
        # require.
        out_of_scope_ids = report["out_of_scope_ids"]
        dequeued = 0
        if out_of_scope_ids:
            stale_queue = Queue.search([
                ("model", "=", self.model_name),
                ("res_id", "in", out_of_scope_ids),
            ])
            dequeued = len(stale_queue)
            stale_queue.unlink()

        to_enqueue = []
        if enqueue:
            already_queued = set(Queue.search([
                ("model", "=", self.model_name),
            ]).mapped("res_id"))
            to_enqueue = [
                rid for rid in report["would_send_ids"]
                if rid not in already_queued
            ]
        if to_enqueue:
            Queue.create([
                {"model": self.model_name, "res_id": rid}
                for rid in to_enqueue
            ])

        _logger.info(
            "sync_index_scope: %s purged=%d dequeued=%d enqueued=%d",
            self.model_name, purged, dequeued, len(to_enqueue),
        )
        return {
            "purged": purged,
            "dequeued": dequeued,
            "enqueued": len(to_enqueue),
        }

    def action_sync_index_scope(self):
        """Settings-page button. Runs ``_sync_index_scope``."""
        self.ensure_one()
        if self.is_global:
            raise UserError(_("Sync index scope applies to a per-model row."))

        # _sync_index_scope refuses internally by returning zeroes,
        # which for an interactive caller is indistinguishable from
        # "already in sync". Say so instead.
        scope_error = self._index_scope_error()
        if scope_error:
            raise UserError(_(
                "The index domain cannot be evaluated, so scope is unknown. "
                "Nothing was changed — no vectors were deleted and nothing "
                "was queued. Fix the domain first.\n\n%s"
            ) % scope_error)

        result = self._sync_index_scope()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Index scope synced"),
                "message": _(
                    "Deleted %(purged)s out-of-scope vector(s) and queued "
                    "%(enqueued)s record(s) for indexing. The cron embeds "
                    "the queued ones on its next pass."
                ) % result,
                "type": "success",
            },
        }

    # -------------------------------------------- daily token budget

    def _token_budget_state(self):
        """``(tokens_used_today, tokens_usage_date)``, read in the
        counter's own transaction.

        Not off ``self``: the counter is written on an independent
        cursor (see ``_token_budget_consume``), and the cron's own
        transaction is a REPEATABLE READ snapshot that will never see
        those commits. Reading through ``self`` would hand back the
        value as it stood when the sweep began, for the whole sweep.

        The flush is what makes that safe. Odoo defers writes per
        field, so a `write` to the counter that has not been flushed
        yet lives in ``env.all.towrite`` and is invisible to SQL —
        the read would return the pre-write value and the cap would
        not bind. Reaching for SQL means taking over the
        synchronisation the ORM would otherwise do for us.
        """
        self.ensure_one()
        self.flush_recordset(["tokens_used_today", "tokens_usage_date"])
        with self.pool.cursor() as cr:
            cr.execute(
                "SELECT tokens_used_today, tokens_usage_date "
                "FROM orc_embedding_config WHERE id = %s",
                (self.id,),
            )
            row = cr.fetchone()
        if not row:
            return 0, None
        return row[0] or 0, row[1]

    def _token_budget_remaining(self):
        """Tokens the cron may still spend today.

        0 means stop. A ``daily_token_cap`` of 0 is the documented
        pause switch (AGENTS.md), so it must read as "spend
        nothing", never as "no limit".

        The cap itself is read off ``self`` — the operator sets it and
        the cron never writes it — while the counter comes from
        ``_token_budget_state``.
        """
        self.ensure_one()
        if self.daily_token_cap <= 0:
            return 0
        used, usage_date = self._token_budget_state()
        if usage_date != fields.Date.context_today(self):
            # A new day. No write needed: the first charge of the day
            # resets the total as part of its own statement.
            used = 0
        return max(0, self.daily_token_cap - used)

    def _token_budget_consume(self, tokens):
        """Add ``tokens`` to today's total, atomically, in a
        transaction of its own.

        Three separate reasons for the shape of this, and dropping
        either one breaks the cap:

        1. **Its own transaction.** A provider charge cannot be
           rolled back, so the record of it must not be either. The
           cron writes this mid-sweep alongside the embeddings; any
           later failure in that pass would unwind the accounting
           while the money stayed spent, and the next pass would
           re-send the same records with the counter back at zero.

        2. **One atomic statement, not read-modify-write.** The
           obvious spelling — read ``self.tokens_used_today``, add,
           write — reads through the *outer* cursor, whose snapshot
           predates every commit this method has made. Each call in a
           sweep would compute the same ``original + this_request``
           and overwrite the last, so a hundred charges would record
           as one. The increment therefore happens inside the
           statement, where it sees its own prior commits, and the
           day-rollover reset folds into the same ``CASE``.

        3. **Flushed first.** Same reason as in
           ``_token_budget_state``, but the consequence is worse: a
           pending ORM write to the counter is flushed *after* this
           statement — ``invalidate_recordset`` below triggers it —
           and overwrites the increment with the stale cached value.
           The charge is then lost while the money stays spent.

        A note for tests: ``pool.cursor()`` is only bound to the test
        transaction inside ``registry.enter_test_mode``, which
        ``HttpCase`` enters and ``TransactionCase`` does not. A
        ``TransactionCase`` exercising this therefore has to enter it
        itself, or the commits here are real ones that outlive
        teardown.
        """
        self.ensure_one()
        if tokens <= 0:
            return
        self.flush_recordset(["tokens_used_today", "tokens_usage_date"])
        with self.pool.cursor() as cr:
            cr.execute(
                """
                UPDATE orc_embedding_config
                   SET tokens_used_today = CASE
                           WHEN tokens_usage_date = %(today)s
                           THEN COALESCE(tokens_used_today, 0)
                           ELSE 0
                       END + %(tokens)s,
                       tokens_usage_date = %(today)s
                 WHERE id = %(id)s
                """,
                {
                    "today": fields.Date.context_today(self),
                    "tokens": tokens,
                    "id": self.id,
                },
            )
        self.invalidate_recordset(["tokens_used_today", "tokens_usage_date"])

    # --------------------------------------------------------- API

    @api.model
    def get_global(self):
        """Return the singleton global config row, raising if missing
        or if the provider key isn't set yet. Callers that need a
        ready-to-use provider config should call this; callers that
        just want the row (e.g. the Settings page) can search
        directly."""
        row = self.search([("is_global", "=", True)], limit=1)
        if not row:
            raise UserError(_(
                "AI Semantic Search global config row missing. Reinstall "
                "the module or recreate it under Settings → Technical → "
                "AI Semantic Search."
            ))
        if not row.provider_api_key:
            raise UserError(_(
                "AI Semantic Search provider API key is not set. Open "
                "Settings → Technical → AI Semantic Search and fill it in."
            ))
        return row

    def action_test_provider(self):
        """Issue a single embed of "ping" and surface auth /
        dimension / latency to the user via a notification.

        Bound to the Settings page button. The error path raises
        a UserError with a readable message instead of silently
        flashing a misleading success.
        """
        self.ensure_one()
        if not self.is_global:
            raise UserError(_("Only the global config row supports this action."))

        if not self.provider_api_key:
            raise UserError(_("Set the provider API key first."))

        provider = OpenAIEmbeddingProvider(
            url=self.provider_url or "https://api.openai.com/v1/embeddings",
            api_key=self.provider_api_key,
            model=self.provider_model or "text-embedding-3-small",
            dim=self.vector_dim or 1536,
        )

        t0 = time.monotonic()
        try:
            vectors = provider.embed(["ping"])
        except Exception as exc:
            raise UserError(_("Provider call failed: %s") % exc) from exc
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if not vectors or not vectors[0]:
            raise UserError(_("Provider returned an empty result."))

        actual_dim = len(vectors[0])
        if actual_dim != self.vector_dim:
            raise UserError(_(
                "Dimension mismatch: provider returned %(actual)s, config "
                "expects %(expected)s. Update Vector dim or change the model."
            ) % {"actual": actual_dim, "expected": self.vector_dim})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Provider OK"),
                "message": _(
                    "%(dim)s-dim vector returned in %(ms)s ms via %(model)s."
                ) % {
                    "dim": actual_dim,
                    "ms": elapsed_ms,
                    "model": self.provider_model,
                },
                "type": "success",
            },
        }

    def action_reindex_all(self):
        """Drop every ``orc.embedding`` row for enabled models and
        enqueue every **in-scope** record. Operator-only; the view
        layer adds a confirmation modal because of the cost
        implication.

        Scope is applied here as well as in the sweep. Not because
        the sweep would miss it — it wouldn't — but because this is
        the one action that can enqueue an entire corpus at once,
        and an operator watching the queue length should see the
        filtered figure rather than the full one followed by a
        silent drain.
        """
        Embedding = self.env["orc.embedding"]
        Queue = self.env["orc.embedding.queue"]

        enabled_rows = self.search([("is_global", "=", False), ("enabled", "=", True)])
        if not enabled_rows:
            raise UserError(_("No enabled models to reindex."))

        # Pre-flight, BEFORE the wipe below. This action deletes the
        # index and then re-enqueues what is in scope — so if scope
        # cannot be determined, the predicate fails closed, nothing is
        # re-enqueued, and the delete has silently destroyed the
        # corpus. Refusing outright is the only version of this that
        # respects "unknown scope means touch nothing".
        broken = {
            row.model_name: row._index_scope_error()
            for row in enabled_rows
            if row._index_scope_error()
        }
        if broken:
            raise UserError(_(
                "Refusing to reindex: the index domain cannot be evaluated "
                "for %(models)s, so scope is unknown and a reindex would "
                "delete the existing index without rebuilding it. Fix the "
                "domain first.\n\n%(details)s"
            ) % {
                "models": ", ".join(sorted(broken)),
                "details": "\n".join(
                    "%s: %s" % (k, v) for k, v in sorted(broken.items())
                ),
            })

        affected_models = enabled_rows.mapped("model_name")

        # Wipe the existing index for those models.
        Embedding.search([("model", "in", affected_models)]).unlink()
        Queue.search([("model", "in", affected_models)]).unlink()

        for cfg in enabled_rows:
            target_model = cfg._scope_target_model()
            if target_model is None:
                _logger.warning(
                    "reindex_all: model %s not installed; skipping.",
                    cfg.model_name,
                )
                continue
            records = target_model.search([])
            in_scope = cfg._filter_indexable(records)
            if len(in_scope) < len(records):
                _logger.info(
                    "reindex_all: %s of %s %s records are out of scope; "
                    "not enqueued.",
                    len(records) - len(in_scope), len(records),
                    cfg.model_name,
                )
            if not in_scope:
                continue
            Queue.create([
                {"model": cfg.model_name, "res_id": rid}
                for rid in in_scope.ids
            ])
            _logger.info(
                "reindex_all: enqueued %s records for %s.",
                len(in_scope), cfg.model_name,
            )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Reindex enqueued"),
                "message": _(
                    "Cleared the index and enqueued every record across "
                    "%(n)s model(s). The cron picks up from here."
                ) % {"n": len(enabled_rows)},
                "type": "success",
            },
        }
