"""ORC API key access audit log (append-only).

One row per RPC method dispatched on a request that was authenticated
with an ORC API key, plus one row per failed key-auth attempt against
an ORC-marked request. Driven by the dispatch wrapper in
``_patches.py`` and the ``_check_credentials`` override in
``models/res_users.py``.

Immutable at three layers: ORM, ACL, Postgres trigger. Pruning happens
via a daily cron that bypasses the trigger only for its own
transaction (``SET LOCAL session_replication_role = replica``).
"""
import logging

from odoo import api, fields, http, models
from odoo.exceptions import UserError

from ._immutable import install_immutable_trigger

_logger = logging.getLogger(__name__)


class OrcApiAccessLog(models.Model):
    _name = "orc.api.access.log"
    _description = "ORC API key access log (append-only)"
    _order = "id DESC"
    _log_access = False

    user_id = fields.Many2one(
        "res.users", string="User", ondelete="set null", index=True, readonly=True,
    )
    login_attempted = fields.Char(
        string="Login attempted",
        readonly=True,
        help="Login the caller asserted in the XML-RPC envelope. Useful "
             "when the uid does not match a real user (failed lookups).",
    )
    endpoint = fields.Char(string="Endpoint", readonly=True, index=True)
    method = fields.Char(string="Method", readonly=True, index=True)
    status = fields.Selection(
        [
            ("ok", "OK"),
            ("denied", "Denied (read-only)"),
            ("failed", "Failed (auth)"),
        ],
        required=True,
        readonly=True,
        index=True,
    )
    denial_reason = fields.Char(readonly=True)
    source_ip = fields.Char(readonly=True)
    user_agent = fields.Char(readonly=True)
    create_date = fields.Datetime(
        string="When",
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
        raise UserError("orc.api.access.log is append-only.")

    def unlink(self):
        raise UserError("orc.api.access.log is append-only.")

    # --- Insertion helper ----------------------------------------------------

    @api.model
    def _record(
        self,
        status,
        endpoint=None,
        method=None,
        user_id=None,
        login_attempted=None,
        denial_reason=None,
    ):
        """Insert a row with HTTP context auto-filled from request."""
        vals = {"status": status}
        if user_id:
            vals["user_id"] = user_id
        if endpoint:
            vals["endpoint"] = endpoint[:255]
        if method:
            vals["method"] = method[:128]
        if login_attempted:
            vals["login_attempted"] = login_attempted[:255]
        if denial_reason:
            vals["denial_reason"] = denial_reason[:255]
        req = http.request
        if req is not None:
            try:
                env_dict = req.httprequest.environ
                # Priority: X-Real-IP (set by the front proxy when it
                # has resolved the true client) -> X-Forwarded-For
                # (chain of hops, first entry is the original client) ->
                # REMOTE_ADDR (raw socket peer; only useful when there's
                # no proxy in front).
                ip = env_dict.get("HTTP_X_REAL_IP")
                if not ip:
                    xff = env_dict.get("HTTP_X_FORWARDED_FOR")
                    if xff:
                        ip = xff.split(",", 1)[0].strip()
                if not ip:
                    ip = env_dict.get("REMOTE_ADDR")
                ua = env_dict.get("HTTP_USER_AGENT")
                if ip:
                    vals["source_ip"] = ip[:64]
                if ua:
                    vals["user_agent"] = ua[:255]
            except Exception:  # pragma: no cover - never fail on logging
                _logger.debug("[orc] could not read HTTP context for access log")
        return self.sudo().create(vals)

    # --- Retention pruning ---------------------------------------------------

    @api.model
    def _cron_orc_access_log_prune(self):
        """Delete rows older than orc.access_log_retention_days.

        The immutability trigger blocks DELETE, so we set
        ``session_replication_role = replica`` for this transaction
        only. That requires the Odoo DB role to have REPLICATION
        privilege - typically the case for the application owner.
        """
        icp = self.env["ir.config_parameter"].sudo()
        try:
            retention_days = int(icp.get_param("orc.access_log_retention_days") or 90)
        except (TypeError, ValueError):
            retention_days = 90
        if retention_days <= 0:
            return
        cr = self.env.cr
        cr.execute("SAVEPOINT orc_access_log_prune")
        try:
            cr.execute("SET LOCAL session_replication_role = replica")
            cr.execute(
                "DELETE FROM orc_api_access_log "
                "WHERE create_date < (now() at time zone 'utc') - %s::interval",
                ["%d days" % retention_days],
            )
            deleted = cr.rowcount
            cr.execute("RELEASE SAVEPOINT orc_access_log_prune")
        except Exception:
            cr.execute("ROLLBACK TO SAVEPOINT orc_access_log_prune")
            raise
        _logger.info(
            "[orc] pruned %d access-log rows older than %d days",
            deleted, retention_days,
        )
