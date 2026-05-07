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
    _description = "ORC semantic search — vector embedding for an Odoo record"
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

    _sql_constraints = [
        (
            "unique_model_res_id",
            "UNIQUE (model, res_id)",
            "Only one embedding row per record.",
        ),
    ]

    # ------------------------------------------------ provider factory

    @api.model
    def _build_provider(self):
        """Return a provider instance from the global config row.

        Test seam: the test suite patches this to inject mocks. Keep
        the factory shape stable (no kwargs from callers).
        """
        cfg = self.env["orc.embedding.config"].get_global()
        return OpenAIEmbeddingProvider(
            url=cfg.provider_url or "https://api.openai.com/v1/embeddings",
            api_key=cfg.provider_api_key,
            model=cfg.provider_model or "text-embedding-3-small",
            dim=cfg.vector_dim or 1536,
        )

    # ------------------------------------------------------------ cron

    @api.model
    def _cron_reindex_sweep(self):
        """Process pending queue rows.

        For each row:
          1. Read the source record.
          2. Extract text per the model's configured extractor.
          3. Truncate to ~8K chars if needed.
          4. Hash; if matches an existing embedding row, drop the
             queue row without calling the provider (hash-skip).
          5. Call provider.embed; upsert the embedding row; drop
             the queue row.
          6. On provider error: leave the queue row, bump attempts,
             store last_error.

        Per-record, no batching. Batching is a v2 optimization.
        """
        Config = self.env["orc.embedding.config"]
        Queue = self.env["orc.embedding.queue"]

        # Build the (model_name → cfg row) map so we don't search
        # per-record. Disabled rows still appear in the queue if a
        # toggle was flipped after enqueue; we drop those queue rows
        # silently rather than processing them.
        configs = {
            c.model_name: c
            for c in Config.search([("is_global", "=", False)])
        }

        # Build provider lazily — only if we have work to do AND
        # the global config has its key. Empty-queue should not
        # raise if the operator hasn't filled the key yet.
        queue_rows = Queue.search([])
        if not queue_rows:
            return

        try:
            provider = self._build_provider()
        except UserError as exc:
            _logger.warning("cron_reindex_sweep: %s", exc)
            return

        processed = 0
        skipped_hash = 0
        errors = 0

        for q in queue_rows:
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

            record = target_model.browse(q.res_id).exists()
            if not record:
                # Source record was deleted between enqueue and
                # sweep. Drop the queue row + any stale embedding.
                q_model, q_res_id = q.model, q.res_id
                q.unlink()
                self.search([
                    ("model", "=", q_model), ("res_id", "=", q_res_id),
                ]).unlink()
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

            raw = record[cfg.text_field_path] if cfg.text_field_path else ""
            text = extractor(raw)

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

            try:
                vectors = provider.embed([text])
            except EmbeddingProviderError as exc:
                q.attempts += 1
                q.last_error = str(exc)
                errors += 1
                _logger.warning(
                    "cron_reindex_sweep: %s/%s provider error (attempt %d): %s",
                    q.model, q.res_id, q.attempts, exc,
                )
                continue

            if not vectors or len(vectors[0]) != provider.dim:
                q.attempts += 1
                q.last_error = "provider returned mis-shaped vector"
                errors += 1
                continue

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

        _logger.info(
            "cron_reindex_sweep: processed=%d errors=%d skipped_hash=%d",
            processed, errors, skipped_hash,
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

        # Resolve which models to search.
        Config = self.env["orc.embedding.config"]
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
        try:
            provider = self._build_provider()
        except UserError:
            # Re-raise the operator-friendly message verbatim.
            raise

        try:
            query_vectors = provider.embed([query])
        except EmbeddingProviderError as exc:
            # Per README "Failure modes": surface as a clean
            # UserError so odoo-mcp wraps it as a tool error.
            raise UserError(_("Embedding provider failed: %s") % exc)

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
