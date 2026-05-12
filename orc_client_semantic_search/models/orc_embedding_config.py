import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..providers.openai import OpenAIEmbeddingProvider


_logger = logging.getLogger(__name__)


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
        help="Cron pauses on overrun and resumes the next day.",
    )

    # ----------------------------- per-model-row fields (indexed)

    model_name = fields.Char(
        string="Odoo model",
        help="e.g. document.page",
        index=True,
    )
    enabled = fields.Boolean(
        string="Enabled",
        default=True,
    )
    text_field_path = fields.Char(
        string="Text field path",
        help="Dotted path to the text source on the record. e.g. 'content'.",
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
        enqueue every record. Operator-only; the view layer adds
        a confirmation modal because of the cost implication.
        """
        Embedding = self.env["orc.embedding"]
        Queue = self.env["orc.embedding.queue"]

        enabled_rows = self.search([("is_global", "=", False), ("enabled", "=", True)])
        if not enabled_rows:
            raise UserError(_("No enabled models to reindex."))

        affected_models = enabled_rows.mapped("model_name")

        # Wipe the existing index for those models.
        Embedding.search([("model", "in", affected_models)]).unlink()
        Queue.search([("model", "in", affected_models)]).unlink()

        for cfg in enabled_rows:
            target_model = self.env.get(cfg.model_name)
            if target_model is None:
                _logger.warning(
                    "reindex_all: model %s not installed; skipping.",
                    cfg.model_name,
                )
                continue
            ids = target_model.with_context(active_test=False).search([]).ids
            if not ids:
                continue
            Queue.create([
                {"model": cfg.model_name, "res_id": rid}
                for rid in ids
            ])
            _logger.info(
                "reindex_all: enqueued %s records for %s.",
                len(ids), cfg.model_name,
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
