import logging

from odoo import api, fields, models

from .orc_embedding_config import EXCLUDE_FIELD


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
#
# All three hooks touch orc.embedding* with `sudo()`: those are
# technical models the end user has no rights on, and the article
# author must not need any. Only the bookkeeping is elevated — the
# article recordset itself stays on the caller's environment, so
# ir.rule keeps deciding what they may write and delete.
class KnowledgeArticle(models.Model):
    _inherit = "knowledge.article"

    orc_ai_index_exclude = fields.Boolean(
        string="Exclude from AI index",
        default=False,
        index=True,
        help="Keep this article out of the semantic-search index. Its "
             "text is then never sent to the embedding provider, and any "
             "vector it already has is deleted on the next indexing "
             "pass. The assistant can still read the article the normal "
             "way if you have access to it — it just can't find it by "
             "meaning.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._orc_enqueue_reindex()
        return records

    def write(self, vals):
        result = super().write(vals)
        # Only enqueue when the indexed text field changed. No
        # config row (or a disabled one) means the model isn't
        # indexed at all, so there is nothing to enqueue. The field
        # comes from the Settings page and is matched against `vals`
        # by name — same shape the cron reads it back with, so a
        # path the rest of the module can't resolve doesn't enqueue
        # here either.
        cfg = self.env["orc.embedding.config"].sudo().search([
            ("is_global", "=", False),
            ("model_name", "=", "knowledge.article"),
            ("enabled", "=", True),
        ], limit=1)
        if not cfg:
            return result

        # Three kinds of write can change what the index should hold:
        #
        # - the indexed text itself;
        # - the exclusion flag. Without it here, ticking the box does
        #   nothing until the body happens to change — and a control
        #   that takes effect at an unrelated later moment is
        #   indistinguishable from one that doesn't work;
        # - any field the configured domain reads. With a domain of
        #   [("category", "!=", "private")], moving an article into
        #   the private category takes it out of scope, and the vector
        #   has to go with it. Nothing about `category` is special —
        #   the fields come from the domain, so they change when the
        #   operator changes it.
        #
        # Both flag directions enqueue: setting it makes the sweep
        # delete the vector, clearing it makes the sweep rebuild it.
        # An enqueue that turns out to be unnecessary costs no
        # provider call — the sweep's hash-skip absorbs it.
        watched = {cfg.text_field_path or "body", EXCLUDE_FIELD}
        watched |= cfg._index_domain_fields()
        if watched & set(vals):
            self._orc_enqueue_reindex()
        return result

    def unlink(self):
        Embedding = self.env["orc.embedding"].sudo()
        Queue = self.env["orc.embedding.queue"].sudo()
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
        """Insert one queue marker per record that needs one.

        Idempotent — the unique constraint on (model, res_id) means a
        second create for an already-queued record is a no-op.

        A record earns a marker if either is true:

        - it is **in scope**, so the sweep should embed it; or
        - it is out of scope but **already has a vector**, so the
          sweep should delete it.

        The second half is what makes exclusion take effect. Filtering
        purely on scope would mean ticking "Exclude from AI index"
        enqueues nothing, and the vector the box is meant to remove
        outlives the decision to remove it. Filtering on neither would
        mean every excluded article gets a marker on every save, for
        the sweep to discard — churn that also hides the useful
        signal in the queue length.
        """
        if not self:
            return
        Queue = self.env["orc.embedding.queue"].sudo()
        Config = self.env["orc.embedding.config"].sudo()

        cfg = Config.search([
            ("is_global", "=", False),
            ("model_name", "=", "knowledge.article"),
        ], limit=1)
        if not cfg:
            return

        in_scope_ids = set(cfg._filter_indexable(self).ids)
        stale_ids = set()
        out_of_scope_ids = [r.id for r in self if r.id not in in_scope_ids]
        if out_of_scope_ids:
            stale_ids = set(self.env["orc.embedding"].sudo().search([
                ("model", "=", "knowledge.article"),
                ("res_id", "in", out_of_scope_ids),
            ]).mapped("res_id"))

        wanted = in_scope_ids | stale_ids
        if not wanted:
            return

        existing_ids = set(Queue.search([
            ("model", "=", "knowledge.article"),
            ("res_id", "in", list(wanted)),
        ]).mapped("res_id"))
        to_create = [
            {"model": "knowledge.article", "res_id": rid}
            for rid in sorted(wanted - existing_ids)
        ]
        if to_create:
            Queue.create(to_create)
