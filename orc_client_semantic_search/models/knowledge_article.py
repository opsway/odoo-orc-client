import logging

from odoo import api, models


_logger = logging.getLogger(__name__)


# Hooks `create`, `write`, and `unlink` on `knowledge.article`.
#
# - create: always enqueue. The cron will hash-skip if the body is
#   identical to a vector that was somehow already there
#   (re-imports, manual fixtures).
# - write: enqueue ONLY when the indexed text fields changed —
#   metadata-only writes (rename, tag toggle) shouldn't burn an
#   embed call. The hash-skip path also catches this, but checking
#   here saves the queue churn.
# - unlink: drop the embedding row. Stale ids in the index would
#   surface as 404s the moment the agent tries to read them.
class KnowledgeArticle(models.Model):
    _inherit = "knowledge.article"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._orc_enqueue_reindex()
        return records

    def write(self, vals):
        result = super().write(vals)
        # Only enqueue when one of the indexed text fields changed.
        # The Settings page allows the operator to set the field
        # path; if it's something we haven't anticipated, fall
        # back to the safe choice of always enqueueing.
        cfg = self.env["orc.embedding.config"].search([
            ("is_global", "=", False),
            ("model_name", "=", "knowledge.article"),
            ("enabled", "=", True),
        ], limit=1)
        if not cfg:
            return result

        watched_field = cfg.text_field_path or "body"
        if watched_field in vals:
            self._orc_enqueue_reindex()
        return result

    def unlink(self):
        Embedding = self.env["orc.embedding"]
        Queue = self.env["orc.embedding.queue"]
        ids = self.ids
        if ids:
            Embedding.search([
                ("model", "=", "knowledge.article"),
                ("res_id", "in", ids),
            ]).unlink()
            Queue.search([
                ("model", "=", "knowledge.article"),
                ("res_id", "in", ids),
            ]).unlink()
        return super().unlink()

    def _orc_enqueue_reindex(self):
        """Insert one queue marker per record. Idempotent — the
        unique constraint on (model, res_id) means a second create
        for an already-queued record is a no-op."""
        if not self:
            return
        Queue = self.env["orc.embedding.queue"]
        existing = Queue.search([
            ("model", "=", "knowledge.article"),
            ("res_id", "in", self.ids),
        ])
        existing_ids = set(existing.mapped("res_id"))
        to_create = [
            {"model": "knowledge.article", "res_id": rec.id}
            for rec in self
            if rec.id not in existing_ids
        ]
        if to_create:
            Queue.create(to_create)
