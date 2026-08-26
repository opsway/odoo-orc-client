from odoo import fields, models


class OrcEmbeddingQueue(models.Model):
    _name = "orc.embedding.queue"
    _description = "AI Workplace semantic search — pending re-index markers"
    _order = "enqueued_at, id"

    model = fields.Char(string="Odoo model", required=True, index=True)
    res_id = fields.Integer(string="Record id", required=True, index=True)
    enqueued_at = fields.Datetime(
        string="Enqueued at", default=fields.Datetime.now, required=True,
    )
    attempts = fields.Integer(string="Attempts", default=0)
    last_error = fields.Text(string="Last error")

    # Odoo 19 dropped `_sql_constraints` — the loader logs
    # "no longer supported" and creates nothing, so on 19.0 these were
    # silently absent. The attribute name supplies the constraint name
    # (`{table}_{attr without leading underscore}`), which reproduces the
    # 18.0 names exactly, so an upgraded database keeps the constraint it
    # already has instead of dropping and re-adding it.
    _unique_model_res_id = models.Constraint(
        "UNIQUE (model, res_id)",
        "A record can have at most one pending queue marker.",
    )
