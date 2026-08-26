import hashlib
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

import numpy as np

from ..providers.base import EmbeddingProviderError
from ..providers.openai import OpenAIEmbeddingProvider
from ..utils import cosine, text_extract


_logger = logging.getLogger(__name__)


# Per the README "Limits" section — see comment on the long-article
# fallback test.
_TEXT_EXCERPT_CAP_CHARS = 8000

# Per the README "API surface" — semantic_search clamps limit at 50.
_SEARCH_MAX_LIMIT = 50


# One row per indexed record. Vector lives in `vector_blob` as
# `numpy.tobytes()` of a float32 array. `content_hash` is the sha256
# of the extracted text and drives hash-skip in the cron worker.
class OrcEmbedding(models.Model):
    _name = "orc.embedding"
    _description = "AI Workplace semantic search — vector embedding for an Odoo record"
    _order = "indexed_at desc, id desc"

    model = fields.Char(string="Odoo model", required=True, index=True)
    res_id = fields.Integer(string="Record id", required=True, index=True)
    vector_blob = fields.Binary(
        string="Vector (numpy float32 bytes)",
        attachment=False,
        help="Stored inline as bytes — see numpy.tobytes() / numpy.frombuffer().",
    )
    content_hash = fields.Char(
        string="Content hash (sha256)",
        size=64,
        index=True,
    )
    text_excerpt_len = fields.Integer(
        string="Text length",
        help="Length (in chars) of the extracted text used for the embedding.",
    )
    indexed_at = fields.Datetime(string="Indexed at")
    provider = fields.Char(
        string="Provider tag",
        help="Provider id at indexing time, e.g. openai:text-embedding-3-small.",
    )

    # Odoo 19 dropped `_sql_constraints` — the loader logs
    # "no longer supported" and creates nothing, so on 19.0 these were
    # silently absent. The attribute name supplies the constraint name
    # (`{table}_{attr without leading underscore}`), which reproduces the
    # 18.0 names exactly, so an upgraded database keeps the constraint it
    # already has instead of dropping and re-adding it.
    _unique_model_res_id = models.Constraint(
        "UNIQUE (model, res_id)",
        "Only one embedding row per record.",
    )

    # ------------------------------------------------ provider factory

    @api.model
    def _build_provider(self):
        """Return a provider instance from the global config row.

        Test seam: the test suite patches this to inject mocks. Keep
        the factory shape stable (no kwargs from callers).

        Read as sudo: semantic_search runs as the end user, who has
        no rights on the config row — and must not get them, since
        it carries the provider API key. The key never leaves this
        method.
        """
        cfg = self.env["orc.embedding.config"].sudo().get_global()
        return OpenAIEmbeddingProvider(
            url=cfg.provider_url or "https://api.openai.com/v1/embeddings",
            api_key=cfg.provider_api_key,
            model=cfg.provider_model or "text-embedding-3-small",
            dim=cfg.vector_dim or 1536,
        )

    @api.model
    def _charge_reported_usage(self, global_cfg, provider):
        """Charge whatever the provider said it billed, and clear it.

        Only for the failure paths: a successful embed charges an
        estimate when the upstream reports nothing, but a failure
        must charge nothing unless the upstream actually said it
        billed something — otherwise a network error, which costs
        nothing, would eat the day's budget.

        Returns the number of tokens charged.
        """
        reported = getattr(provider, "last_usage_tokens", None)
        if not isinstance(reported, int) or reported <= 0:
            return 0
        global_cfg._token_budget_consume(reported)
        provider.last_usage_tokens = None
        return reported

    # ------------------------------------------------------------ cron

    @api.model
    # pylint: disable=too-many-branches,too-many-statements
    def _cron_reindex_sweep(self):
        """Process pending queue rows.

        For each row:
          1. Read the source record.
          2. Check scope. Out of scope → delete any embedding row,
             drop the queue row, next. This is the authoritative
             gate on what may be sent to the provider; see README
             "Index scope".
          3. Extract text per the model's configured extractor.
          4. Truncate to ~8K chars if needed.
          5. Hash; if matches an existing embedding row, drop the
             queue row without calling the provider (hash-skip).
          6. Check today's token budget. Exhausted → leave the row
             queued for tomorrow.
          7. Call provider.embed; charge the budget; upsert the
             embedding row; drop the queue row.
          8. On provider error: leave the queue row, bump attempts,
             store last_error.

        Steps 1–5 cost nothing, so they run even when the budget is
        spent or no API key is set: cleanup must not be blocked on a
        budget, or ticking "exclude" would silently wait for a refill.

        Per-record, no batching. Batching is a v2 optimization.
        """
        Config = self.env["orc.embedding.config"].sudo()
        Queue = self.env["orc.embedding.queue"].sudo()

        # Build the (model_name → cfg row) map so we don't search
        # per-record. Disabled rows still appear in the queue if a
        # toggle was flipped after enqueue; we drop those queue rows
        # silently rather than processing them.
        configs = {
            c.model_name: c
            for c in Config.search([("is_global", "=", False)])
        }
        # One health check per config per pass, not per queue row. A
        # model whose domain no longer evaluates is left strictly
        # alone this pass: not embedded (we can't say it's in scope)
        # and not purged (we can't say it isn't).
        scope_errors = {
            name: cfg._index_scope_error()
            for name, cfg in configs.items()
        }

        queue_rows = Queue.search([])
        if not queue_rows:
            return

        global_cfg = Config.search([("is_global", "=", True)], limit=1)
        if not global_cfg:
            _logger.warning(
                "cron_reindex_sweep: no global config row; nothing to do.",
            )
            return

        # The provider is built on first need, not up front. A pass
        # that only has purging to do must still do it — with no key
        # set, and with the budget spent.
        provider = None
        budget = global_cfg._token_budget_remaining()
        if budget <= 0:
            used_so_far, _d = global_cfg._token_budget_state()
            _logger.info(
                "cron_reindex_sweep: daily token cap reached or paused "
                "(cap=%s used=%s); embedding is skipped this pass, "
                "out-of-scope cleanup still runs.",
                global_cfg.daily_token_cap, used_so_far,
            )

        processed = 0
        skipped_hash = 0
        errors = 0
        purged_scope = 0
        deferred_budget = 0
        spent_this_pass = 0

        broken_scope = 0

        for q in queue_rows:
            if scope_errors.get(q.model):
                # Leave the row exactly where it is. Retried next pass;
                # fixed by fixing the domain.
                broken_scope += 1
                continue

            cfg = configs.get(q.model)
            if cfg is None or not cfg.enabled:
                # Stale queue row for a model that's no longer
                # indexed. Drop it rather than letting it pile up.
                q.unlink()
                continue

            target_model = self.env.get(q.model)
            if target_model is None:
                _logger.warning(
                    "cron_reindex_sweep: model %s not installed; "
                    "dropping queue row.", q.model,
                )
                q.unlink()
                continue

            record = target_model.sudo().browse(q.res_id).exists()
            if not record:
                # Source record was deleted between enqueue and
                # sweep. Drop the queue row + any stale embedding.
                q_model, q_res_id = q.model, q.res_id
                q.unlink()
                self.search([
                    ("model", "=", q_model), ("res_id", "=", q_res_id),
                ]).unlink()
                continue

            # THE scope gate. Everything upstream of here — the
            # create/write hooks, Reindex all, Sync index scope — is
            # an optimization: a queue row can outlive the settings
            # that created it, so a domain tightened while rows are
            # pending has to be honoured here or not at all.
            #
            # Deliberately placed before the text extraction and the
            # budget check: dropping an out-of-scope vector is free,
            # and must not wait for either.
            if not cfg._filter_indexable(record):
                q_model, q_res_id = q.model, q.res_id
                q.unlink()
                removed = self.search([
                    ("model", "=", q_model), ("res_id", "=", q_res_id),
                ])
                if removed:
                    removed.unlink()
                    purged_scope += 1
                continue

            # Extract text via the model's configured extractor.
            extractor = text_extract.EXTRACTORS.get(cfg.text_extractor)
            if extractor is None:
                _logger.warning(
                    "cron_reindex_sweep: unknown extractor %s; "
                    "dropping queue row.", cfg.text_extractor,
                )
                q.unlink()
                continue

            # A configured field can stop existing — a module upgrade
            # renames or drops it — and the raise would abort the whole
            # pass, not just this row. Charge it to the row instead, so
            # one broken config doesn't stop every other model from
            # being indexed.
            try:
                raw = record[cfg.text_field_path] if cfg.text_field_path else ""
                text = extractor(raw)
            except Exception as exc:
                q.attempts += 1
                q.last_error = "text_field_path %r unreadable: %s" % (
                    cfg.text_field_path, exc,
                )
                errors += 1
                _logger.error(
                    "cron_reindex_sweep: %s/%s text_field_path %r is not "
                    "readable (%s); leaving the row queued.",
                    q.model, q.res_id, cfg.text_field_path, exc,
                )
                continue

            if not text:
                # Nothing to embed; remove any stale embedding and
                # drop the queue row.
                q_model, q_res_id = q.model, q.res_id
                q.unlink()
                self.search([
                    ("model", "=", q_model), ("res_id", "=", q_res_id),
                ]).unlink()
                continue

            # Long-article fallback: truncate to the cap.
            if len(text) > _TEXT_EXCERPT_CAP_CHARS:
                _logger.info(
                    "cron_reindex_sweep: %s/%s text %d chars > %d; "
                    "embedding first %d chars only.",
                    q.model, q.res_id, len(text),
                    _TEXT_EXCERPT_CAP_CHARS, _TEXT_EXCERPT_CAP_CHARS,
                )
                text = text[:_TEXT_EXCERPT_CAP_CHARS]

            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

            existing = self.search([
                ("model", "=", q.model), ("res_id", "=", q.res_id),
            ], limit=1)
            if existing and existing.content_hash == digest:
                # Same content as previously embedded — no provider
                # call, just drop the queue marker.
                q.unlink()
                skipped_hash += 1
                continue

            # From here on the row costs money. Everything above was
            # free, which is why the budget is checked here and not
            # at the top of the pass.
            if budget <= 0:
                deferred_budget += 1
                continue

            if provider is None:
                try:
                    provider = self._build_provider()
                except UserError as exc:
                    # No key, or no global row. Nothing embeddable
                    # can succeed this pass; the purges already done
                    # stand.
                    _logger.warning("cron_reindex_sweep: %s", exc)
                    break

            try:
                vectors = provider.embed([text])
            except EmbeddingProviderError as exc:
                # A failure is not necessarily a free failure. The
                # request can reach the upstream, be billed, and only
                # then fail our own validation — a wrong `vector_dim`
                # does exactly that. Charging it is what stops a
                # misconfiguration from spending without limit: there
                # is no attempt ceiling, so the row would otherwise be
                # re-billed on every pass forever with the counter
                # sitting at zero.
                billed = self._charge_reported_usage(global_cfg, provider)
                budget -= billed
                spent_this_pass += billed
                q.attempts += 1
                q.last_error = str(exc)
                errors += 1
                _logger.warning(
                    "cron_reindex_sweep: %s/%s provider error (attempt %d, "
                    "billed %s tokens): %s",
                    q.model, q.res_id, q.attempts, billed, exc,
                )
                continue

            if not vectors or len(vectors[0]) != provider.dim:
                billed = self._charge_reported_usage(global_cfg, provider)
                budget -= billed
                spent_this_pass += billed
                q.attempts += 1
                q.last_error = "provider returned mis-shaped vector"
                errors += 1
                continue

            # Charge the day's budget. Prefer what the provider says
            # it billed; fall back to the conventional chars÷4
            # estimate when it reports nothing (or reports something
            # that isn't a number, as a test double will).
            reported = getattr(provider, "last_usage_tokens", None)
            spent = (
                reported if isinstance(reported, int) and reported > 0
                else max(1, len(text) // 4)
            )
            global_cfg._token_budget_consume(spent)
            budget -= spent
            spent_this_pass += spent
            provider.last_usage_tokens = None

            vec = np.array(vectors[0], dtype=np.float32)
            row_vals = {
                "vector_blob": vec.tobytes(),
                "content_hash": digest,
                "text_excerpt_len": len(text),
                "indexed_at": fields.Datetime.now(),
                "provider": provider.provider_tag(),
            }
            if existing:
                existing.write(row_vals)
            else:
                self.create({
                    "model": q.model,
                    "res_id": q.res_id,
                    **row_vals,
                })
            q.unlink()
            processed += 1

        if broken_scope:
            _logger.error(
                "cron_reindex_sweep: %d queue row(s) skipped entirely — the "
                "index domain no longer evaluates: %s",
                broken_scope,
                {k: v for k, v in scope_errors.items() if v},
            )

        # Read the day's total back through the counter's own cursor.
        # `global_cfg.tokens_used_today` would report the value as it
        # stood when this pass began — the sweep's snapshot cannot see
        # the independent commits.
        used_today, _usage_date = global_cfg._token_budget_state()
        _logger.info(
            "cron_reindex_sweep: processed=%d errors=%d skipped_hash=%d "
            "purged_out_of_scope=%d deferred_no_budget=%d "
            "skipped_broken_scope=%d spent_this_pass=%d tokens_today=%d/%d",
            processed, errors, skipped_hash, purged_scope, deferred_budget,
            broken_scope, spent_this_pass,
            used_today, global_cfg.daily_token_cap,
        )

    # --------------------------------------------------- public search

    @api.model
    def semantic_search(self, query, models=None, limit=10):
        """Cosine-rank stored vectors against the query embedding.

        Returns ``[{model, id, score}]`` — refs only. See README
        "API surface" for the contract and "Permission model" for
        why we don't surface titles or snippets.
        """
        if not query or not isinstance(query, str) or not query.strip():
            raise UserError(_("Query must be a non-empty string."))

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, _SEARCH_MAX_LIMIT))

        # Resolve which models to search. The caller is the end
        # user; the config rows are operator-only, so read them
        # elevated. Only model names leave this block.
        Config = self.env["orc.embedding.config"].sudo()
        enabled_cfgs = Config.search([
            ("is_global", "=", False), ("enabled", "=", True),
        ])
        enabled_models = enabled_cfgs.mapped("model_name")
        if models:
            target_models = [m for m in models if m in enabled_models]
        else:
            target_models = enabled_models

        if not target_models:
            return []

        # Embed the query using the same provider as the corpus.
        # _build_provider raises UserError directly with the
        # operator-friendly message — no extra wrapping needed.
        provider = self._build_provider()

        try:
            query_vectors = provider.embed([query])
        except EmbeddingProviderError as exc:
            # Per README "Failure modes": surface as a clean
            # UserError so odoo-mcp wraps it as a tool error.
            raise UserError(
                _("Embedding provider failed: %s") % exc
            ) from exc

        if not query_vectors or len(query_vectors[0]) != provider.dim:
            raise UserError(_(
                "Embedding provider returned an unexpected response shape."
            ))

        query_vec = np.array(query_vectors[0], dtype=np.float32)

        # Pull every embedding row in scope. For corpora < 100K this
        # reads in a few hundred ms; cosine is the cheap part.
        rows = self.search([("model", "in", target_models)])
        if not rows:
            return []

        candidates = []
        for r in rows:
            if not r.vector_blob:
                continue
            vec = np.frombuffer(r.vector_blob, dtype=np.float32)
            if vec.shape[0] != provider.dim:
                # Stale row from a prior provider/model with a
                # different dim. Skip silently — the cron will
                # repair it on the next write.
                continue
            candidates.append((r.model, r.res_id, vec))

        ranked = cosine.top_k(query_vec, candidates, limit=limit)
        return [
            {"model": m, "id": rid, "score": s}
            for (m, rid, s) in ranked
        ]
