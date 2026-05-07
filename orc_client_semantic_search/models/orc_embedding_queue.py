from odoo import fields, models


class OrcEmbeddingQueue(models.Model):
    _name = "orc.embedding.queue"
    _description = "ORC semantic search — pending re-index markers"
    _order = "enqueued_at, id"

    model = fields.Char(string="Odoo model", required=True, index=True)
    res_id = fields.Integer(string="Record id", required=True, index=True)
    enqueued_at = fields.Datetime(
        string="Enqueued at", default=fields.Datetime.now, required=True,
    )
    attempts = fields.Integer(string="Attempts", default=0)
    last_error = fields.Text(string="Last error")

    _sql_constraints = [
        (
            "unique_model_res_id",
            "UNIQUE (model, res_id)",
            "A record can have at most one pending queue marker.",
        ),
    ]
