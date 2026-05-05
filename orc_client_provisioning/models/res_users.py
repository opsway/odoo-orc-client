"""ORC provisioning fields and lifecycle on res.users.

Design departures from the Odoo 18 version:

* The Odoo API key lives directly on ``res.users`` (hash + leading-hex
  index) - there is no separate ``res.users.apikeys`` model in Odoo 13
  to inherit, and one key per user matches the addon's actual usage.
* RPC auth via the key only fires when the inbound request carries the
  ``X-ORC-Auth`` header. Password auth for everyone else is untouched.
* Every successful key-auth and every failed key-auth attempt is
  recorded in the immutable ``orc.api.access.log``.
"""
import binascii
import logging
import os
from datetime import timedelta

import passlib.context

from odoo import _, api, fields, http, models
from odoo.exceptions import AccessDenied, UserError

_logger = logging.getLogger(__name__)

ORC_KEY_NAME = "ORC (auto-managed)"
API_KEY_SIZE = 20  # raw bytes -> 40 hex chars
INDEX_SIZE = 8  # leading hex chars used as the lookup index

# 6000 rounds is what upstream apikeys uses; brute-force resistance is
# already strong because the keys are 160 bits of urandom, not user
# secrets.
KEY_CRYPT_CONTEXT = passlib.context.CryptContext(
    ["pbkdf2_sha512"], pbkdf2_sha512__rounds=6000,
)
_hash_key = getattr(KEY_CRYPT_CONTEXT, "hash", None) or KEY_CRYPT_CONTEXT.encrypt


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
    orc_api_key_hash = fields.Char(
        string="ORC API key (hashed)",
        readonly=True,
        copy=False,
        groups="orc_client_provisioning.group_orc_manager",
    )
    orc_api_key_index = fields.Char(
        string="ORC API key index",
        size=INDEX_SIZE,
        index=True,
        readonly=True,
        copy=False,
        groups="orc_client_provisioning.group_orc_manager",
    )
    orc_api_key_expires_at = fields.Datetime(
        string="ORC API key expires",
        readonly=True,
        copy=False,
    )
    orc_api_key_rotated_at = fields.Datetime(
        string="ORC API key rotated",
        readonly=True,
        copy=False,
    )
    orc_access_level = fields.Selection(
        [("read", "Read only"), ("write", "Read / Write")],
        string="ORC API access level",
        default="read",
        help=(
            "Controls what the generated ORC API key is allowed to do "
            "over XML-RPC / JSON-RPC. 'Read only' (default) lets the "
            "agent inspect data without mutating anything. "
            "Ignored for ORC managers - they are always provisioned as "
            "ORC admins with full access."
        ),
    )
    orc_is_manager = fields.Boolean(
        string="Is ORC manager",
        compute="_compute_orc_is_manager",
        help=(
            "True when the user belongs to the ORC manager group. "
            "Drives the ORC-side role: managers provision as admin; "
            "everyone else as user."
        ),
    )

    @api.depends("groups_id")
    def _compute_orc_is_manager(self):
        group = self.env.ref(
            "orc_client_provisioning.group_orc_manager",
            raise_if_not_found=False,
        )
        for user in self:
            user.orc_is_manager = bool(group and group in user.groups_id)

    # --- Auth: header-gated ORC API-key path ---------------------------------

    def _check_credentials(self, password):
        """Try the ORC API-key path when the request is header-marked.

        Header convention: any inbound XML-RPC / JSON-RPC request that
        carries ``X-ORC-Auth`` is asserting "I am ORC; please match my
        password against the API key on this user". On match we set
        request flags so the dispatch wrapper (in ``_patches.py``) knows
        to log every method call and enforce the read-only allowlist.

        Without the header, the upstream password check runs unchanged.
        With the header but a bad key, we log the failure and raise -
        we never fall back to password auth on header-marked requests.
        """
        req = http.request
        if not req or not req.httprequest.headers.get("X-ORC-Auth"):
            return super()._check_credentials(password)

        # Header-marked. Verify against the stored ORC key for self.
        if self._orc_verify_api_key(password):
            req.orc_api_key_authenticated = True
            req.orc_api_key_readonly = (self.orc_access_level == "read")
            self.env["orc.api.access.log"].sudo()._record(
                status="ok",
                user_id=self.env.uid,
                login_attempted=self.env.user.login,
                method="_check_credentials",
                endpoint="auth",
            )
            return
        # Bad key on a header-marked request. Log + deny.
        self.env["orc.api.access.log"].sudo()._record(
            status="failed",
            user_id=self.env.uid,
            login_attempted=getattr(self.env.user, "login", None),
            method="_check_credentials",
            endpoint="auth",
            denial_reason="invalid-key",
        )
        raise AccessDenied()

    def _orc_verify_api_key(self, candidate):
        """Verify ``candidate`` against this user's stored ORC key.

        Returns True iff:
          * the user has an active ORC key (hash + index set),
          * the key is not expired,
          * the leading-hex index matches,
          * the slow pbkdf2 verify succeeds.

        ``hash`` / ``index`` are manager-restricted (``groups=`` on the
        fields), so we read via ``sudo()`` - the user authenticating
        here is themselves, and ``_check_credentials`` cannot bootstrap
        the manager group on their behalf.
        """
        self.ensure_one()
        if not candidate or not isinstance(candidate, str):
            return False
        s = self.sudo()
        if not s.orc_api_key_hash or not s.orc_api_key_index:
            return False
        if s.orc_api_key_expires_at and s.orc_api_key_expires_at < fields.Datetime.now():
            return False
        if candidate[:INDEX_SIZE] != s.orc_api_key_index:
            return False
        try:
            return bool(KEY_CRYPT_CONTEXT.verify(candidate, s.orc_api_key_hash))
        except Exception:
            _logger.exception("[orc] crypt-verify error for uid=%s", self.id)
            return False

    # --- Provisioning lifecycle ----------------------------------------------

    def _orc_desired_role(self):
        """Pick the ORC role this user should hold.

        ORC has exactly two primary roles: ``admin`` and ``user``. The
        read-only / read-write distinction is a separate capability
        pushed via ``push_odoo_key(access_level=...)``.
        """
        self.ensure_one()
        return "admin" if self.orc_is_manager else "user"

    def _orc_desired_access(self):
        """Effective Odoo-RPC capability for this user's key.

        Managers always get ``write`` (radio is hidden in the form for
        them). For non-managers, the local ``orc_access_level`` selector
        is the source of truth.
        """
        self.ensure_one()
        if self.orc_is_manager:
            return "write"
        return "read" if self.orc_access_level == "read" else "write"

    def _orc_generate_api_key(self):
        """Mint a new ORC key for this user. Returns the raw value once.

        Side effect: persists the hash, index, rotation timestamp and
        expiry on ``self``. The caller is responsible for the network
        side (push to ORC) and for revoking the old key on success.
        """
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        try:
            rotation_days = int(icp.get_param("orc.rotation_days") or 30)
        except (TypeError, ValueError):
            rotation_days = 30
        raw = binascii.hexlify(os.urandom(API_KEY_SIZE)).decode()
        now = fields.Datetime.now()
        self.sudo().write({
            "orc_api_key_hash": _hash_key(raw),
            "orc_api_key_index": raw[:INDEX_SIZE],
            "orc_api_key_rotated_at": now,
            "orc_api_key_expires_at": now + timedelta(days=rotation_days),
        })
        return raw

    def _orc_clear_api_key(self):
        self.sudo().write({
            "orc_api_key_hash": False,
            "orc_api_key_index": False,
            "orc_api_key_rotated_at": False,
            "orc_api_key_expires_at": False,
        })

    def action_orc_provision(self):
        """Provision / re-provision this user in ORC.

        Order (zero-downtime, leak-resistant):
          1. Snapshot the old key fields (in-memory only - they're
             about to be overwritten).
          2. Mint NEW key locally.
          3. Create user in ORC (idempotent).
          4. Push NEW key to ORC.
          5. Any failure between (2) and (4) -> raise; the surrounding
             transaction rolls back, restoring the old key fields.

        Step 5 means ORC ends up with whichever key Odoo currently
        believes is canonical. If the push succeeds but the cron
        framework rolls back later for unrelated reasons, the next
        rotation overwrites the orphan on the ORC side via upsert.
        """
        for user in self:
            if not user.active:
                continue
            client = self.env["orc.client"]
            old_uid = user.orc_user_id

            new_raw_key = user._orc_generate_api_key()

            try:
                desired_role = user._orc_desired_role()
                orc_uid = client.provision_user(
                    email=user.login,
                    name=user.name or user.login,
                    role=desired_role,
                )
                if not user.orc_user_id:
                    user.sudo().write({"orc_user_id": orc_uid})

                effective_level = user._orc_desired_access()
                client.push_odoo_key(
                    email=user.login,
                    api_key=new_raw_key,
                    access_level=effective_level,
                )
            except Exception:
                # Don't try to clean up - the surrounding transaction
                # rolls back and the old key fields come back. Just
                # propagate.
                raise

            if not user.orc_provisioned_at:
                user.sudo().write({"orc_provisioned_at": fields.Datetime.now()})

            self.env["orc.audit.log"].sudo()._record(
                user_id=user.id,
                action="provision" if not old_uid else "rotate",
                status="ok",
            )

    def action_orc_deprovision(self):
        for user in self:
            if not user.orc_user_id:
                continue
            client = self.env["orc.client"]
            try:
                client.deprovision_user(user_id=user.orc_user_id)
            except UserError as exc:
                self.env["orc.audit.log"].sudo()._record(
                    user_id=user.id,
                    action="deprovision",
                    status="error",
                    error=str(exc),
                )
                raise

            user._orc_clear_api_key()
            user.sudo().write({
                "orc_enabled": False,
                "orc_user_id": False,
                "orc_provisioned_at": False,
            })
            self.env["orc.audit.log"].sudo()._record(
                user_id=user.id,
                action="deprovision",
                status="ok",
            )

    # --- Toggle hook ---------------------------------------------------------

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

    # --- Crons ---------------------------------------------------------------

    @api.model
    def _cron_orc_rotate_keys(self):
        """Rotate keys whose expiry has passed.

        With per-user storage there's no orphan to clean up; expiry
        comparison on ``orc_api_key_expires_at`` is enough.
        """
        cutoff = fields.Datetime.now()
        due = self.search([
            ("orc_enabled", "=", True),
            ("orc_user_id", "!=", False),
            "|",
                ("orc_api_key_expires_at", "=", False),
                ("orc_api_key_expires_at", "<", cutoff),
        ])
        for user in due:
            try:
                user.action_orc_provision()
            except Exception as exc:
                _logger.warning("[orc] rotation failed for %s: %s", user.login, exc)
                self.env["orc.audit.log"].sudo()._record(
                    user_id=user.id,
                    action="rotate",
                    status="error",
                    error=str(exc),
                )

    @api.model
    def _cron_orc_reconcile(self):
        """Reassert authority for tier and capability drift.

        * Admin vs user *tier*: Odoo group is authoritative.
        * Read vs write *capability* within the user tier: ORC is
          authoritative; we mirror it locally and re-provision.
        """
        client = self.env["orc.client"]
        try:
            data = client.list_users()
        except UserError as exc:
            _logger.warning("[orc] reconcile: %s", exc)
            return
        remote_users = {
            u.get("email"): u
            for u in data.get("users", [])
            if u.get("email")
        }
        local_enabled = self.search([("orc_enabled", "=", True)])
        local_emails = {u.login for u in local_enabled}

        drift_remote_only = set(remote_users) - local_emails
        drift_local_only = local_emails - set(remote_users)
        if drift_remote_only or drift_local_only:
            self.env["orc.audit.log"].sudo()._record(
                action="reconcile",
                status="drift",
                error=("remote-only: %s local-only: %s"
                       % (sorted(drift_remote_only), sorted(drift_local_only)))[:1000],
            )

        for user in local_enabled:
            remote = remote_users.get(user.login)
            if not remote:
                continue
            remote_role = (remote.get("role") or "").strip()
            if not remote_role:
                continue
            remote_access = (remote.get("odoo_access") or "").strip()
            if remote_role == "user_readonly":
                remote_role = "user"
                remote_access = remote_access or "read"

            odoo_is_admin = user.orc_is_manager
            orc_is_admin = remote_role == "admin"
            if odoo_is_admin != orc_is_admin:
                _logger.info(
                    "[orc] tier drift for %s: odoo_manager=%s orc_role=%s - re-provisioning",
                    user.login, odoo_is_admin, remote_role,
                )
                try:
                    user.action_orc_provision()
                except Exception as exc:
                    _logger.warning(
                        "[orc] tier-drift re-provision failed for %s: %s",
                        user.login, exc,
                    )
                    self.env["orc.audit.log"].sudo()._record(
                        user_id=user.id,
                        action="rotate",
                        status="error",
                        error="tier-drift re-provision: %s" % exc,
                    )
                continue

            if odoo_is_admin:
                continue

            if not remote_access:
                continue
            expected_level = remote_access if remote_access in ("read", "write") else None
            if not expected_level or user.orc_access_level == expected_level:
                continue
            _logger.info(
                "[orc] capability drift for %s: odoo_access=%s (local %s) - rotating",
                user.login, expected_level, user.orc_access_level,
            )
            user.sudo().write({"orc_access_level": expected_level})
            try:
                user.action_orc_provision()
            except Exception as exc:
                _logger.warning(
                    "[orc] capability-drift rotation failed for %s: %s",
                    user.login, exc,
                )
                self.env["orc.audit.log"].sudo()._record(
                    user_id=user.id,
                    action="rotate",
                    status="error",
                    error="capability-drift rotation: %s" % exc,
                )

    @api.model
    def _cron_orc_sync(self):
        """Hourly. Reconcile + role-drift rotation."""
        self._cron_orc_reconcile()

    @api.model
    def _cron_orc_maintenance(self):
        """Daily. Expiry-driven rotation."""
        self._cron_orc_rotate_keys()

    # --- UI helper -----------------------------------------------------------

    def action_orc_view_access_log(self):
        """Open the access log filtered to this user."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("ORC API access log"),
            "res_model": "orc.api.access.log",
            "view_mode": "tree,form",
            "domain": [("user_id", "=", self.id)],
        }
