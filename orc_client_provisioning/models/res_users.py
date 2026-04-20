import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ORC_KEY_NAME = "ORC (auto-managed)"


class ResUsers(models.Model):
    _inherit = "res.users"

    orc_enabled = fields.Boolean(
        string="ORC access",
        default=False,
        help=(
            "When enabled, the user is provisioned into OpsWay ORC, "
            "gets an auto-managed Odoo API key pushed to ORC, and sees "
            "the systray icon to open their ORC conversations."
        ),
    )
    orc_user_id = fields.Char(
        string="ORC user id",
        readonly=True,
        copy=False,
    )
    orc_provisioned_at = fields.Datetime(
        string="ORC provisioned",
        readonly=True,
        copy=False,
    )
    orc_last_rotation_at = fields.Datetime(
        string="ORC key rotated",
        readonly=True,
        copy=False,
    )
    orc_api_key_id = fields.Many2one(
        "res.users.apikeys",
        string="ORC API key",
        readonly=True,
        ondelete="set null",
        copy=False,
    )

    # --- Provisioning lifecycle ------------------------------------------------

    def _orc_generate_api_key(self):
        """Generate a new Odoo API key for this user, tagged as ORC-managed."""
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        rotation_days = int(icp.get_param("orc.rotation_days") or 30)
        expiration = fields.Datetime.add(fields.Datetime.now(), days=rotation_days)
        try:
            raw_key = (
                self.env["res.users.apikeys"]
                .with_user(self)
                .sudo()
                ._generate(scope=None, name=ORC_KEY_NAME, expiration_date=expiration)
            )
        except Exception as exc:
            _logger.exception("[orc] _generate failed for %s", self.login)
            raise UserError(_(
                "Failed to generate Odoo API key for %(login)s: %(err)s"
            ) % {"login": self.login, "err": exc}) from exc

        # `_generate()` returns the raw key and persists a row; find
        # the freshly created row by name+user and pin our reference.
        key_row = self.env["res.users.apikeys"].sudo().search(
            [("user_id", "=", self.id), ("name", "=", ORC_KEY_NAME)],
            order="create_date DESC",
            limit=1,
        )
        return raw_key, key_row

    def _orc_revoke_key(self, key_record):
        if key_record and key_record.exists():
            try:
                key_record.sudo().unlink()
            except Exception as exc:
                _logger.warning("[orc] failed to revoke key %s: %s", key_record.id, exc)

    def action_orc_provision(self):
        """Provision / re-provision this user in ORC.

        Ordering (zero-downtime on re-run):
          1. Generate NEW key locally.
          2. Create user in ORC (idempotent — 200 if already exists).
          3. Push NEW key to ORC (upsert semantics in user_odoo_keys).
          4. Revoke OLD key only AFTER (2) + (3) succeeded.

        Any exception between (1) and (3) rolls back the Odoo TX; the
        just-created key is garbage-collected by the orphan-cleanup cron.
        """
        for user in self:
            if not user.active:
                continue
            client = self.env["orc.client"]

            # 1. New key first (old still valid).
            new_raw_key, new_key_row = user._orc_generate_api_key()
            old_key_row = user.orc_api_key_id

            try:
                # 2. Ensure user exists in ORC. Capture user_id on first create.
                if not user.orc_user_id:
                    orc_uid = client.provision_user(
                        email=user.login,
                        name=user.name or user.login,
                        role="user",
                    )
                    user.sudo().write({"orc_user_id": orc_uid})

                # 3. Push the new key.
                client.push_odoo_key(email=user.login, api_key=new_raw_key)
            except Exception:
                # Rollback the just-created key so we don't leak it.
                user._orc_revoke_key(new_key_row)
                raise

            # 4. Revoke old key (if any). Best-effort — its presence
            #    won't leak access now that ORC has the new one, but we
            #    remove it to cap blast radius.
            if old_key_row and old_key_row.id != new_key_row.id:
                user._orc_revoke_key(old_key_row)

            now = fields.Datetime.now()
            user.sudo().write({
                "orc_api_key_id": new_key_row.id,
                "orc_provisioned_at": user.orc_provisioned_at or now,
                "orc_last_rotation_at": now,
            })

            self.env["orc.audit.log"].sudo().create({
                "user_id": user.id,
                "action": "provision" if not old_key_row else "rotate",
                "status": "ok",
            })

    def action_orc_deprovision(self):
        for user in self:
            if not user.orc_user_id:
                continue
            client = self.env["orc.client"]
            try:
                client.deprovision_user(user_id=user.orc_user_id)
            except UserError as exc:
                self.env["orc.audit.log"].sudo().create({
                    "user_id": user.id,
                    "action": "deprovision",
                    "status": "error",
                    "error": str(exc),
                })
                raise

            user._orc_revoke_key(user.orc_api_key_id)
            user.sudo().write({
                "orc_enabled": False,
                "orc_user_id": False,
                "orc_api_key_id": False,
                "orc_provisioned_at": False,
                "orc_last_rotation_at": False,
            })
            self.env["orc.audit.log"].sudo().create({
                "user_id": user.id,
                "action": "deprovision",
                "status": "ok",
            })

    # --- Toggle hook -----------------------------------------------------------

    def write(self, vals):
        if "orc_enabled" not in vals:
            return super().write(vals)

        flip_to = vals["orc_enabled"]
        res = super().write(vals)
        for user in self:
            if flip_to and not user.orc_user_id:
                user.action_orc_provision()
            elif not flip_to and user.orc_user_id:
                user.action_orc_deprovision()
        return res

    # --- Crons -----------------------------------------------------------------

    @api.model
    def _cron_orc_rotate_keys(self):
        """Rotate keys older than orc.rotation_days. Runs daily."""
        icp = self.env["ir.config_parameter"].sudo()
        rotation_days = int(icp.get_param("orc.rotation_days") or 30)
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=rotation_days)
        due = self.search([
            ("orc_enabled", "=", True),
            ("orc_user_id", "!=", False),
            "|",
                ("orc_last_rotation_at", "=", False),
                ("orc_last_rotation_at", "<", cutoff),
        ])
        for user in due:
            try:
                user.action_orc_provision()
            except Exception as exc:
                _logger.warning("[orc] rotation failed for %s: %s", user.login, exc)
                self.env["orc.audit.log"].sudo().create({
                    "user_id": user.id,
                    "action": "rotate",
                    "status": "error",
                    "error": str(exc),
                })

    @api.model
    def _cron_orc_reconcile(self):
        """Compare ORC's view of enrolled users with Odoo's. Log drift."""
        client = self.env["orc.client"]
        try:
            data = client.list_users()
        except UserError as exc:
            _logger.warning("[orc] reconcile: %s", exc)
            return
        remote_emails = {u.get("email") for u in data.get("users", []) if u.get("email")}
        local_enabled = self.search([("orc_enabled", "=", True)])
        local_emails = {u.login for u in local_enabled}

        drift_remote_only = remote_emails - local_emails
        drift_local_only = local_emails - remote_emails
        if drift_remote_only or drift_local_only:
            self.env["orc.audit.log"].sudo().create({
                "action": "reconcile",
                "status": "drift",
                "error": (
                    f"remote-only: {sorted(drift_remote_only)} "
                    f"local-only: {sorted(drift_local_only)}"
                )[:1000],
            })

    @api.model
    def _cron_orc_orphan_cleanup(self):
        """Revoke ORC-tagged api keys not referenced by any res.users."""
        keys = self.env["res.users.apikeys"].sudo().search([("name", "=", ORC_KEY_NAME)])
        referenced_ids = set(self.search([("orc_api_key_id", "!=", False)]).mapped("orc_api_key_id.id"))
        for k in keys:
            if k.id not in referenced_ids:
                _logger.info("[orc] revoking orphan key %s (user=%s)", k.id, k.user_id.login)
                try:
                    k.unlink()
                except Exception as exc:
                    _logger.warning("[orc] orphan revoke failed: %s", exc)
