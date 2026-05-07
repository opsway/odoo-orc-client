"""Provisioning lifecycle audit log (append-only).

Records every provision, rotate, deprovision, sso, reconcile event the
addon performs. Immutable at three layers: ORM (write/unlink raise),
ACL (no write/create/unlink for any group), and Postgres trigger
(BEFORE UPDATE OR DELETE on the table raises). The trigger function is
shared with ``orc.api.access.log`` and is installed lazily by whichever
log model's ``init()`` runs first.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from ._immutable import install_immutable_trigger

_logger = logging.getLogger(__name__)


class OrcAuditLog(models.Model):
    _name = "orc.audit.log"
    _description = "ORC provisioning audit log (append-only)"
    _order = "id DESC"
    _rec_name = "action"
    _log_access = False  # implies mutability; we have none.

    user_id = fields.Many2one(
        "res.users", string="User", ondelete="set null", index=True, readonly=True,
    )
    action = fields.Selection(
        [
            ("provision", "Provision"),
            ("rotate", "Rotate"),
            ("deprovision", "Deprovision"),
            ("sso", "SSO handoff"),
            ("reconcile", "Reconcile"),
            ("orphan_remote_user", "Orphan remote user"),
        ],
        required=True,
        readonly=True,
    )
    status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("drift", "Drift")],
        default="ok",
        required=True,
        readonly=True,
    )
    error = fields.Text(readonly=True)
    create_date = fields.Datetime(
        string="Created",
        default=fields.Datetime.now,
        readonly=True,
        index=True,
    )

    def init(self):
        super_init = getattr(super(), "init", None)
        if callable(super_init):
            super_init()
        install_immutable_trigger(self.env.cr, self._table)

    # --- Immutability backstops at the ORM layer -----------------------------

    def write(self, vals):
        raise UserError("orc.audit.log is append-only.")

    def unlink(self):
        raise UserError("orc.audit.log is append-only.")

    @api.model
    def _record(self, action, status="ok", user_id=None, error=None):
        """Idiomatic insertion helper. Use this instead of plain create()
        so callers don't have to remember the field names.
        """
        vals = {"action": action, "status": status}
        if user_id:
            vals["user_id"] = user_id
        if error:
            vals["error"] = error[:8000] if isinstance(error, str) else error
        return self.sudo().create(vals)
