from odoo import fields, models


class OrcAuditLog(models.Model):
    _name = "orc.audit.log"
    _description = "ORC provisioning audit log"
    _order = "create_date DESC"
    _rec_name = "action"

    user_id = fields.Many2one("res.users", string="User", ondelete="set null", index=True)
    action = fields.Selection(
        [
            ("provision", "Provision"),
            ("rotate", "Rotate"),
            ("deprovision", "Deprovision"),
            ("sso", "SSO handoff"),
            ("reconcile", "Reconcile"),
        ],
        required=True,
    )
    status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("drift", "Drift")],
        default="ok",
        required=True,
    )
    error = fields.Text(readonly=True)
