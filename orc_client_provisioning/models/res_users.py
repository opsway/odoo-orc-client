"""ORC provisioning fields and lifecycle on res.users.

Design departures from the Odoo 18 version (kept v13-specific):

* The Odoo API key lives directly on ``res.users`` (hash + leading-hex
  index) - there is no separate ``res.users.apikeys`` model in Odoo 13
  to inherit, and one key per user matches the addon's actual usage.
* RPC auth via the key only fires when the inbound request carries the
  ``X-ORC-Auth`` header. Password auth for everyone else is untouched.
* Every successful key-auth and every failed key-auth attempt is
  recorded in the immutable ``orc.api.access.log``.

Aligned with v18 main:

* ``_orc_desired_role`` always returns ``"member"`` - admin promotion
  is an ORC-dashboard action, not Odoo's authority.
* No read/write capability split. Every ORC-managed key has the user's
  full Odoo permissions; the request-scoped read-only allowlist is
  gone (see ``models/base.py`` and ``_patches.py``).
* ``_cron_orc_reconcile`` is a true two-way membership sync; sync
  status is stamped on each user row via ``_orc_stamp_sync``.
* Unticking ``orc_enabled`` is per-infra revoke (keeps ``orc_user_id``
  as a breadcrumb so re-tick recovers the same ORC identity).
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
    orc_is_manager = fields.Boolean(
        string="Is ORC manager",
        compute="_compute_orc_is_manager",
        help=(
            "True when the user belongs to the ORC manager group. "
            "Drives form affordances; admin promotion is an "
            "ORC-dashboard action, not Odoo's authority."
        ),
    )
    # Per-user observability for the reconcile cron + write-on-flip
    # path. Stamped by every cron pass and the write() override -
    # NULL means "never synced." Surfaced on the user form so admins
    # can tell at a glance whether the cron picked up a recent flip
    # of orc_enabled.
    orc_last_sync_at = fields.Datetime(
        string="Last synced at",
        readonly=True,
        copy=False,
    )
    orc_last_sync_status = fields.Selection(
        selection=[
            ("ok", "OK"),
            ("drift", "Drift"),
            ("error", "Error"),
        ],
        string="Last sync status",
        readonly=True,
        copy=False,
    )
    orc_last_sync_message = fields.Char(
        string="Last sync message",
        readonly=True,
        copy=False,
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
        password against the API key on this user". On match we set a
        request flag so the dispatch wrapper (in ``_patches.py``) knows
        to log every method call.

        Without the header, the upstream password check runs unchanged.
        With the header but a bad key, we log the failure and raise -
        we never fall back to password auth on header-marked requests.
        """
        req = http.request
        if not req or not req.httprequest.headers.get("X-ORC-Auth"):
            super()._check_credentials(password)
            return

        # Header-marked. Verify against the stored ORC key for self.
        if self._orc_verify_api_key(password):
            req.orc_api_key_authenticated = True
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
        """Role sent to ORC on provision.

        The addon only provisions ``member`` - admin promotion happens
        in the ORC dashboard, not here. ``orc_is_manager`` still drives
        view affordances but no longer auto-promotes the user to ORC
        admin.
        """
        self.ensure_one()
        return "member"

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

    def _orc_stamp_sync(self, status, message=""):
        """Stamp the last-sync triple on this recordset.

        Always called from a cron's per-user try/except so an exception
        here never bubbles up. Truncates the message so a long stack
        trace doesn't blow out the column.
        """
        self.sudo().write({
            "orc_last_sync_at": fields.Datetime.now(),
            "orc_last_sync_status": status,
            "orc_last_sync_message": (message or "")[:240],
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

            # If anything below raises, the surrounding transaction
            # rolls back and the old key fields come back; nothing to
            # clean up on the Odoo side.
            desired_role = user._orc_desired_role()
            orc_uid = client.provision_user(
                email=user.login,
                name=user.name or user.login,
                role=desired_role,
            )
            if not user.orc_user_id:
                user.sudo().write({"orc_user_id": orc_uid})

            # Push the new Odoo API key. ORC stores it encrypted; the
            # agent will use it to call Odoo tools as this user.
            # ``odoo_login`` covers the case where login != email
            # (e.g. the built-in ``admin`` user) - ORC needs the exact
            # login string to authenticate back into Odoo.
            client.push_odoo_key(
                email=user.login,
                api_key=new_raw_key,
                odoo_login=user.login,
            )

            if not user.orc_provisioned_at:
                user.sudo().write({"orc_provisioned_at": fields.Datetime.now()})

            self.env["orc.audit.log"].sudo()._record(
                user_id=user.id,
                action="provision" if not old_uid else "rotate",
                status="ok",
            )

    def action_orc_deprovision(self):
        """Revoke this user's access on THIS Odoo instance only.

        Per-infra revoke pattern: drop the local key fields and tell
        ORC to delete the matching ``user_odoo_keys`` row + the
        ``infrastructure.member`` engine relation. The user's
        organization membership and historical task rooms stay intact
        on the ORC side; full offboarding is a dashboard action.

        ``orc_user_id`` and ``orc_provisioned_at`` are preserved as
        breadcrumbs so re-ticking ``orc_enabled`` later replays
        provisioning against the same ORC identity rather than
        creating a new one (``provision_user`` is idempotent ORC-side).
        """
        for user in self:
            if not user.orc_user_id:
                continue
            client = self.env["orc.client"]
            try:
                client.revoke_infra_access(email=user.login)
            except UserError as exc:
                self.env["orc.audit.log"].sudo()._record(
                    user_id=user.id,
                    action="deprovision",
                    status="error",
                    error=str(exc),
                )
                raise

            user._orc_clear_api_key()
            self.env["orc.audit.log"].sudo()._record(
                user_id=user.id,
                action="deprovision",
                status="ok",
            )

    # --- Toggle hook ---------------------------------------------------------

    # Re-entry guard. The (de)provision flows write back to res.users
    # to record their bookkeeping (orc_user_id, orc_last_sync_*); without
    # a marker the write override below would re-trigger them and
    # recurse forever. Anything tagged with this context bypasses the
    # provisioning logic and just persists the row.
    _ORC_INFLIGHT_CTX = "orc_provisioning_inflight"

    def write(self, vals):
        if (
            "orc_enabled" not in vals
            or self.env.context.get(self._ORC_INFLIGHT_CTX)
        ):
            return super().write(vals)

        flip_to = vals["orc_enabled"]
        # Mark the cascade so action_orc_provision / action_orc_deprovision's
        # internal writes (orc_user_id, orc_api_key_*, orc_last_sync_*, and
        # in deprovision orc_enabled itself) can persist without re-entering.
        self_inflight = self.with_context(**{self._ORC_INFLIGHT_CTX: True})
        res = super(ResUsers, self_inflight).write(vals)
        for user in self_inflight:
            if flip_to and not user.orc_user_id:
                user.action_orc_provision()
                user._orc_stamp_sync("ok", "provisioned on save")
            elif not flip_to and user.orc_user_id:
                user.action_orc_deprovision()
                user._orc_stamp_sync("ok", "deprovisioned on save")
        return res

    # --- Crons ---------------------------------------------------------------

    @api.model
    def _cron_orc_rotate_keys(self):
        """Rotate keys whose expiry has passed.

        With per-user storage there's no orphan to clean up; expiry
        comparison on ``orc_api_key_expires_at`` is enough. Every
        per-user branch stamps ``orc_last_sync_*`` so the form view
        reflects the rotation outcome.
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
                user._orc_stamp_sync("ok", "key rotated")
            except Exception as exc:
                _logger.warning("[orc] rotation failed for %s: %s", user.login, exc)
                user._orc_stamp_sync("error", "rotation failed: %s" % exc)
                self.env["orc.audit.log"].sudo()._record(
                    user_id=user.id,
                    action="rotate",
                    status="error",
                    error=str(exc),
                )

    @api.model
    def _cron_orc_reconcile(self):
        """Two-way reconcile: local Odoo is the source of truth.

        Per email in (local_enabled u remote):
          - local_enabled + remote present  -> in sync (stamp ok)
          - local_enabled + remote missing  -> re-provision to ORC
          - remote present + local disabled -> revoke from ORC
          - remote present + no local user  -> orphan; audit-log only

        Each per-user branch wraps the work in its own try/except and
        always stamps ``orc_last_sync_*`` so admins can see staleness
        on the user form. A failure in ``client.list_users()`` itself
        stamps every ``orc_enabled=True`` user as error so the
        dashboard surfaces a network/auth outage immediately.
        """
        client = self.env["orc.client"]
        local_enabled = self.search([("orc_enabled", "=", True)])

        try:
            data = client.list_users()
        except UserError as exc:
            _logger.warning("[orc] reconcile fetch failed: %s", exc)
            for user in local_enabled:
                user._orc_stamp_sync("error", "reconcile fetch failed: %s" % exc)
            self.env["orc.audit.log"].sudo()._record(
                action="reconcile",
                status="error",
                error=str(exc)[:1000],
            )
            return

        remote_users = {
            u.get("email"): u
            for u in data.get("users", [])
            if u.get("email")
        }
        local_by_email = {u.login: u for u in local_enabled}

        # Direction A - local enabled, sync forward.
        for email, user in local_by_email.items():
            if email in remote_users:
                user._orc_stamp_sync("ok", "in sync")
                continue
            try:
                user.action_orc_provision()
                user._orc_stamp_sync("ok", "re-provisioned to ORC")
            except Exception as exc:
                _logger.warning(
                    "[orc] reconcile re-provision failed for %s: %s",
                    user.login, exc,
                )
                user._orc_stamp_sync("error", "re-provision failed: %s" % exc)
                self.env["orc.audit.log"].sudo()._record(
                    user_id=user.id,
                    action="reconcile",
                    status="error",
                    error=str(exc)[:1000],
                )

        # Direction B - remote present without a corresponding local
        # ``orc_enabled=True`` row. Two sub-cases:
        #   1. Local user exists with orc_enabled=False -> revoke.
        #   2. No local user at all -> orphan, log only (we don't
        #      auto-create res.users from the remote list).
        residual_remote = set(remote_users) - set(local_by_email)
        if residual_remote:
            local_disabled_matches = self.search([
                ("login", "in", list(residual_remote)),
                ("orc_enabled", "=", False),
            ])
            disabled_by_email = {u.login: u for u in local_disabled_matches}
            for email in residual_remote:
                user = disabled_by_email.get(email)
                if user is None:
                    self.env["orc.audit.log"].sudo()._record(
                        action="orphan_remote_user",
                        status="drift",
                        error=("no local res.users for %s" % email)[:1000],
                    )
                    continue
                try:
                    client.revoke_infra_access(email=email)
                    user._orc_stamp_sync("ok", "deprovisioned from ORC")
                except Exception as exc:
                    _logger.warning(
                        "[orc] reconcile deprovision failed for %s: %s",
                        email, exc,
                    )
                    user._orc_stamp_sync("error", "deprovision failed: %s" % exc)
                    self.env["orc.audit.log"].sudo()._record(
                        user_id=user.id,
                        action="reconcile",
                        status="error",
                        error=str(exc)[:1000],
                    )

    @api.model
    def _cron_orc_sync(self):
        """Hourly. Fast, safe, idempotent.

        Runs the two-way reconcile so a flip of ``orc_enabled`` on
        either side propagates within <= 1 hour without waiting for
        the regular rotation-by-expiration schedule.
        """
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
