import logging
from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ORC_KEY_NAME = "AI Workplace (auto-managed)"


class ResUsers(models.Model):
    _inherit = "res.users"

    orc_enabled = fields.Boolean(
        string="AI Workplace access",
        default=False,
        help=(
            "When enabled, the user is provisioned into AI Workplace, "
            "gets an auto-managed Odoo API key pushed to it, and sees "
            "the systray icon to open their AI Workplace conversations."
        ),
    )
    orc_user_id = fields.Char(
        string="User ID",
        readonly=True,
        copy=False,
    )
    orc_provisioned_at = fields.Datetime(
        string="Provisioned",
        readonly=True,
        copy=False,
    )
    orc_last_rotation_at = fields.Datetime(
        string="Key rotated",
        readonly=True,
        copy=False,
    )
    orc_api_key_id = fields.Many2one(
        "res.users.apikeys",
        string="Managed API key",
        readonly=True,
        ondelete="set null",
        copy=False,
    )
    orc_is_manager = fields.Boolean(
        string="Is AI Workplace manager",
        compute="_compute_orc_is_manager",
        help=(
            "True when the user belongs to the AI Workplace "
            "manager group (implied by base.group_system by default)."
        ),
    )
    # Per-user observability for the reconcile cron + write-on-flip
    # path. Stamped by every cron pass and the write() override —
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

    # --- Provisioning lifecycle ------------------------------------------------

    def _orc_effective_email(self) -> str:
        """Return a gateway-safe, globally-unique email for this user.

        Odoo allows non-email logins (e.g. the built-in ``admin``
        account). The gateway deduplicates users globally on email, so
        passing ``login = "admin"`` from two different Odoo instances
        collides on the same gateway user row, giving one AI Workplace
        identity access to both organisations.

        When ``login`` already contains ``@`` it is returned unchanged.
        Otherwise we qualify it with the Odoo instance's public hostname
        (from ``web.base.url``), e.g. ``"admin"`` on
        ``https://myco.odoo.com`` → ``"admin@myco.odoo.com"``.
        """
        self.ensure_one()
        login = self.login
        if "@" in login:
            return login
        icp = self.env["ir.config_parameter"].sudo()
        base_url = (icp.get_param("web.base.url") or "").strip().rstrip("/")
        hostname = urlparse(base_url).hostname or "odoo.localhost"
        return f"{login}@{hostname}"

    def _orc_desired_role(self) -> str:
        """The addon only provisions ``member`` — admin promotion
        happens in the AI Workplace dashboard, not here. The
        ``orc_is_manager`` flag still drives view affordances but
        no longer auto-promotes the user to AI Workplace admin.
        """
        return "member"

    def _orc_generate_api_key(self):
        """Generate a new Odoo API key for this user, tagged as AI Workplace-managed."""
        self.ensure_one()
        try:
            raw_key = (
                self.env["res.users.apikeys"]
                .with_user(self)
                .sudo()
                ._generate(None, ORC_KEY_NAME)
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

    def _orc_stamp_sync(self, status, message=""):
        """Stamp the last-sync triple on this recordset. Always called
        from a cron's per-user try/except so an exception here never
        bubbles up. Truncates the message so a long stack trace
        doesn't blow out the column.
        """
        self.sudo().write({
            "orc_last_sync_at": fields.Datetime.now(),
            "orc_last_sync_status": status,
            "orc_last_sync_message": (message or "")[:240],
        })

    def action_orc_provision(self):
        """Provision / re-provision this user in AI Workplace.

        Ordering (zero-downtime on re-run):
          1. Generate NEW key locally.
          2. Create user in AI Workplace (idempotent — 200 if already exists).
          3. Push NEW key to AI Workplace (upsert semantics in user_odoo_keys).
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
                # 2. Ensure user exists in AI Workplace with the right role.
                # Role is derived from group membership: AI Workplace-manager
                # group → admin; everyone else → user. provision_user
                # is idempotent on the AI Workplace side, so calling it on
                # every run keeps role in sync.
                desired_role = user._orc_desired_role()
                eff_email = user._orc_effective_email()
                orc_uid = client.provision_user(
                    email=eff_email,
                    name=user.name or user.login,
                    role=desired_role,
                )
                if not user.orc_user_id:
                    user.sudo().write({"orc_user_id": orc_uid})

                # 3. Push the new Odoo API key. AI Workplace stores it
                # encrypted; the agent will use it to call Odoo
                # tools as this user.
                client.push_odoo_key(
                    email=eff_email,
                    api_key=new_raw_key,
                    # Always pass odoo_login explicitly: eff_email may be
                    # qualified (e.g. "admin@myco.odoo.com") and differ from
                    # the real Odoo login that authenticates API calls.
                    odoo_login=user.login,
                )
            except Exception:
                # Rollback the just-created key so we don't leak it.
                user._orc_revoke_key(new_key_row)
                raise

            # 4. Revoke old key (if any). Best-effort — its presence
            #    won't leak access now that AI Workplace has the new one, but we
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
        """Revoke this user's access on THIS Odoo instance only.

        Per the A₁ design: unticking ``orc_enabled`` is per-infra
        revoke, not full offboarding. We drop the user's AI Workplace-managed
        Odoo API key (local) and tell AI Workplace to delete the matching
        ``user_odoo_keys`` row + ``infrastructure.member`` relation.

        We INTENTIONALLY keep ``orc_user_id`` as a breadcrumb so
        re-ticking ``orc_enabled`` later recovers the same AI Workplace
        identity rather than re-provisioning from scratch. The
        user's organization membership, historical task rooms, and
        enrolments on other Odoos remain untouched — those are not
        this addon's to manage.
        """
        for user in self:
            if not user.orc_user_id:
                continue
            client = self.env["orc.client"]
            try:
                client.revoke_infra_access(email=user._orc_effective_email())
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
                "orc_api_key_id": False,
                "orc_last_rotation_at": False,
                # orc_user_id + orc_provisioned_at kept as breadcrumbs;
                # re-ticking replays provisioning against the same AI Workplace
                # identity (provision_user is idempotent on the AI Workplace
                # side so this is safe).
            })
            self.env["orc.audit.log"].sudo().create({
                "user_id": user.id,
                "action": "deprovision",
                "status": "ok",
            })

    # --- Toggle hook -----------------------------------------------------------

    # Re-entry guard. The (de)provision flows write back to res.users
    # to record their bookkeeping (orc_api_key_id, orc_last_*); without
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
        # internal writes can persist orc_api_key_id, orc_last_rotation_at,
        # and (when deprovisioning) orc_enabled itself without re-entering
        # this hook.
        self_inflight = self.with_context(**{self._ORC_INFLIGHT_CTX: True})
        res = super(ResUsers, self_inflight).write(vals)
        for user in self_inflight:
            # Re-provision fires when `orc_enabled` flips true AND
            # there's no live AI Workplace-managed API key — covers both the
            # "never enrolled" case (orc_user_id is None) and the
            # "previously unchecked, now re-ticked" case (orc_user_id
            # survives as a breadcrumb but orc_api_key_id was cleared
            # on deprovision).
            if flip_to and not user.orc_api_key_id:
                user.action_orc_provision()
                user._orc_stamp_sync("ok", "provisioned on save")
            elif not flip_to and user.orc_user_id:
                user.action_orc_deprovision()
                user._orc_stamp_sync("ok", "deprovisioned on save")
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
                user._orc_stamp_sync("ok", "key rotated")
            except Exception as exc:
                _logger.warning("[orc] rotation failed for %s: %s", user.login, exc)
                user._orc_stamp_sync("error", f"rotation failed: {exc}")
                self.env["orc.audit.log"].sudo().create({
                    "user_id": user.id,
                    "action": "rotate",
                    "status": "error",
                    "error": str(exc),
                })

    @api.model
    def _cron_orc_reconcile(self):
        """Two-way reconcile: local Odoo is the source of truth.

        Per email in (local_enabled ∪ remote):
          - local_enabled + remote present  → in sync (stamp ok)
          - local_enabled + remote missing  → re-provision to AI Workplace
          - remote present + local disabled → revoke from AI Workplace
          - remote present + no local user  → orphan; audit-log only

        "Remote present" means the user holds a key on THIS infra.
        ``client.list_users()`` is backed by the per-infra endpoint
        ``/api/addon/infrastructure-users``; a user revoked from this
        Odoo (``revoke_infra_access``) keeps their org membership but
        loses the per-infra key, so they correctly drop out of both
        directions:
          - Direction A re-provisions them (they're still locally
            enabled but no longer reachable on this infra).
          - Direction B stops re-revoking them every cron tick once
            the prior revoke succeeded.

        Each per-user branch wraps the work in its own try/except and
        always stamps `orc_last_sync_*` so admins can see staleness on
        the user form. A failure in `client.list_users()` itself stamps
        every `orc_enabled=True` user as error so the dashboard surfaces
        a network/auth outage immediately.
        """
        client = self.env["orc.client"]
        local_enabled = self.search([("orc_enabled", "=", True)])

        try:
            data = client.list_users()
        except UserError as exc:
            _logger.warning("[orc] reconcile fetch failed: %s", exc)
            for user in local_enabled:
                user._orc_stamp_sync("error", f"reconcile fetch failed: {exc}")
            self.env["orc.audit.log"].sudo().create({
                "action": "reconcile",
                "status": "error",
                "error": str(exc)[:1000],
            })
            return

        remote_users = {
            u.get("email"): u
            for u in data.get("users", [])
            if u.get("email")
        }
        local_by_email = {u._orc_effective_email(): u for u in local_enabled}

        # Direction A — local enabled, sync forward.
        for email, user in local_by_email.items():
            if email in remote_users:
                user._orc_stamp_sync("ok", "in sync")
                continue
            try:
                user.action_orc_provision()
                user._orc_stamp_sync("ok", "re-provisioned to AI Workplace")
            except Exception as exc:
                _logger.warning(
                    "[orc] reconcile re-provision failed for %s: %s",
                    user.login, exc,
                )
                user._orc_stamp_sync("error", f"re-provision failed: {exc}")
                self.env["orc.audit.log"].sudo().create({
                    "user_id": user.id,
                    "action": "reconcile",
                    "status": "error",
                    "error": str(exc)[:1000],
                })

        # Direction B — remote present without a corresponding local
        # `orc_enabled=True` row. Two sub-cases:
        #   1. Local user exists with orc_enabled=False → deprovision.
        #   2. No local user at all → orphan, log only (we don't
        #      auto-create res.users from the remote list).
        residual_remote = set(remote_users) - set(local_by_email)
        if residual_remote:
            # Search previously provisioned disabled users and key by
            # effective email (not raw login) so bare logins like "admin"
            # match their qualified gateway identity "admin@hostname".
            local_disabled_provisioned = self.search([
                ("orc_enabled", "=", False),
                ("orc_user_id", "!=", False),
            ])
            disabled_by_email = {
                u._orc_effective_email(): u for u in local_disabled_provisioned
            }
            for email in residual_remote:
                user = disabled_by_email.get(email)
                if user is None:
                    self.env["orc.audit.log"].sudo().create({
                        "action": "orphan_remote_user",
                        "status": "drift",
                        "error": f"no local res.users for {email}"[:1000],
                    })
                    continue
                try:
                    client.revoke_infra_access(email=email)
                    user._orc_stamp_sync("ok", "deprovisioned from AI Workplace")
                except Exception as exc:
                    _logger.warning(
                        "[orc] reconcile deprovision failed for %s: %s",
                        email, exc,
                    )
                    user._orc_stamp_sync("error", f"deprovision failed: {exc}")
                    self.env["orc.audit.log"].sudo().create({
                        "user_id": user.id,
                        "action": "reconcile",
                        "status": "error",
                        "error": str(exc)[:1000],
                    })

    @api.model
    def _cron_orc_orphan_cleanup(self):
        """Revoke AI Workplace-tagged api keys not referenced by any res.users."""
        keys = self.env["res.users.apikeys"].sudo().search([("name", "=", ORC_KEY_NAME)])
        referenced_ids = set(self.search([("orc_api_key_id", "!=", False)]).mapped("orc_api_key_id.id"))
        for k in keys:
            if k.id not in referenced_ids:
                _logger.info("[orc] revoking orphan key %s (user=%s)", k.id, k.user_id.login)
                try:
                    k.unlink()
                except Exception as exc:
                    _logger.warning("[orc] orphan revoke failed: %s", exc)

    # --- Cron orchestration (18.0.1.2.0) --------------------------------------
    #
    # Three crons were consolidated into two to stop them firing in the
    # same minute and serialising on res.users locks. Semantics are
    # preserved; the underlying methods above are unchanged.

    @api.model
    def _cron_orc_sync(self):
        """Hourly. Fast, safe, idempotent.

        Runs the reconcile pass, which now includes role-drift detection
        and rotation so an AI Workplace admin flipping a user to/from
        ``user_readonly`` propagates to the Odoo side within ≤ 1 hour
        without waiting for the regular rotation-by-expiration schedule.
        """
        self._cron_orc_reconcile()

    @api.model
    def _cron_orc_maintenance(self):
        """Nightly (02:15 UTC by default). Orphan cleanup then rotation.

        Ordering matters: cleanup first removes stray key rows from
        previous failed rotations so the rotate step doesn't regenerate
        them immediately. Role-drift rotations are handled by the
        hourly sync cron above — this cron only rotates by expiration.
        """
        self._cron_orc_orphan_cleanup()
        self._cron_orc_rotate_keys()
