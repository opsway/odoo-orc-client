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
    orc_gateway_email = fields.Char(
        string="Gateway email",
        readonly=True,
        copy=False,
        help=(
            "The email address under which this user is registered in the "
            "AI Workplace gateway (set at first provision). May differ from "
            "login for bare-login accounts (e.g. 'admin'). Used for all "
            "subsequent gateway calls (revoke, SSO, tasks) so that the "
            "identity stays stable even if the qualification logic changes."
        ),
    )
    # Plan §9 + task 63 — gate enrolment on a non-empty Odoo login.
    # Odoo's res.users.login is a required field at the DB level
    # (NOT NULL), so the computed value is True for every persisted
    # user.  The gate primarily exists to (a) document the
    # precondition in the UI, (b) protect against future Odoo
    # versions that relax the NOT NULL, and (c) give the form view
    # an attribute to bind `readonly` on.  An empty/null login
    # would be an invalid (pinned_org_id, odoo_login) key on the
    # gateway side and break the iframe SSO lookup.
    orc_provisionable = fields.Boolean(
        string="Provisionable",
        compute="_compute_orc_provisionable",
        help=(
            "True when the user has a non-empty Odoo login that can be "
            "used as the AI Workplace per-org identity key. Required "
            "before the AI Workplace access checkbox can be toggled on."
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

    @api.depends("login")
    def _compute_orc_provisionable(self):
        for user in self:
            user.orc_provisionable = bool((user.login or "").strip())

    # --- Login-change guard (plan §9.2 + §9.3) ---------------------------------
    #
    # The (pinned_org_id, odoo_login) gateway identity assumes a stable
    # login string.  Renaming a user's Odoo login while orc_enabled=True
    # would (a) silently mint a NEW gateway-side user row on the next
    # reconcile under the new login, leaking the prior identity, and
    # (b) leave the prior row dangling with no Odoo counterpart.  Both
    # branches force orc_enabled off on login change so the admin
    # consciously re-enables (which then re-provisions cleanly).
    #
    # onchange is the client-side hint (drops the checkbox in the UI as
    # soon as the login field changes).  The write() override below is
    # the server-side enforcement — onchange is only fired by the form
    # view, so an XML-RPC or scripted write that flips login + leaves
    # orc_enabled=True in the same call needs the server guard too.

    @api.onchange("login")
    def _onchange_login_clear_orc_enabled(self):
        for user in self:
            if user.orc_enabled:
                user.orc_enabled = False

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

    def _orc_gateway_identity(self) -> str:
        """Return the email the gateway already knows this user by.

        Uses the stored ``orc_gateway_email`` (written at first provision).
        Falls back to raw ``login`` for users provisioned before
        ``orc_gateway_email`` was introduced — the gateway still holds
        them under their bare login (e.g. ``"admin"``).

        Use this for every operation against an already-provisioned user
        (revoke, SSO, tasks, reconcile). Use ``_orc_effective_email()``
        only when creating/updating the gateway registration.
        """
        self.ensure_one()
        return self.orc_gateway_email or self.login

    def _orc_generate_api_key(self):
        """Generate a new Odoo API key for this user, tagged as AI Workplace-managed.

        The key is created **and committed in its own cursor** before we
        return it. This is load-bearing: the caller immediately pushes the raw
        key to AI Workplace, whose ``POST /api/auth/setup-key`` validates it by
        connecting BACK into Odoo over XML-RPC on a *separate* connection
        (ORC #304). That probe runs READ COMMITTED, so it only sees the key if
        it is already committed — a key created in the still-open save/cron
        transaction is invisible to the probe and gets rejected ("wrong key or
        login"), which then rolls the whole save back. Committing here makes
        the row durable + visible to the probe regardless of the enclosing
        transaction; on a failed push the caller revokes it durably (see
        ``_orc_revoke_key(..., commit=True)``), with the orphan-cleanup cron as
        the backstop.

        Returns ``(raw_key, new_key_id)`` — the id is captured inside the
        generating cursor and returned as a plain int, because the caller's
        REPEATABLE READ snapshot cannot see the just-committed row.
        """
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        rotation_days = int(icp.get_param("orc.rotation_days") or 30)
        expiration = fields.Datetime.add(fields.Datetime.now(), days=rotation_days)
        try:
            # Own cursor → commits on clean exit, so the row is visible to AI
            # Workplace's cross-connection probe before the caller pushes it.
            with self.env.registry.cursor() as key_cr:
                key_env = api.Environment(key_cr, self.env.uid, self.env.context)
                raw_key = (
                    key_env["res.users.apikeys"]
                    .with_user(self.id)
                    .sudo()
                    ._generate(scope=None, name=ORC_KEY_NAME, expiration_date=expiration)
                )
                # Capture the id HERE, inside the generating cursor, where the
                # row is unambiguously visible. Re-searching from the *outer*
                # transaction (as this used to) silently returns EMPTY: Odoo
                # runs cursors at REPEATABLE READ, so the caller's snapshot —
                # opened before this nested cursor committed — cannot see the
                # new row. That empty result made the caller store
                # `orc_api_key_id = False`; the nightly orphan-cleanup cron then
                # reaped the now-unreferenced key out from under AI Workplace,
                # breaking the user's Odoo access on every rotation while the
                # gateway kept the (now dead) key.
                new_key_id = (
                    key_env["res.users.apikeys"]
                    .sudo()
                    .search(
                        [("user_id", "=", self.id), ("name", "=", ORC_KEY_NAME)],
                        order="create_date DESC",
                        limit=1,
                    )
                    .id
                )
        except Exception as exc:
            _logger.exception("[orc] _generate failed for %s", self.login)
            raise UserError(_(
                "Failed to generate Odoo API key for %(login)s: %(err)s"
            ) % {"login": self.login, "err": exc}) from exc

        # Return the id (not a recordset): the row is committed but invisible to
        # the caller's snapshot, so a recordset read here would be empty. The id
        # is a plain int the caller stores directly into orc_api_key_id
        # (res.users.apikeys is _auto=False → no FK existence check on write).
        return raw_key, new_key_id

    def _orc_revoke_key(self, key_ref, commit=False):
        """Revoke an Odoo API key. ``key_ref`` is either a
        ``res.users.apikeys`` recordset (the OLD key, visible in the caller's
        transaction) or a bare int id (the freshly generated key, committed in
        its own cursor and therefore NOT visible under the caller's REPEATABLE
        READ snapshot — so we must NOT gate on a caller-side ``.exists()``).
        """
        key_id = key_ref if isinstance(key_ref, int) else (key_ref.id if key_ref else False)
        if not key_id:
            return
        if commit:
            # The new key is committed in its own cursor (so the probe can see
            # it), so unlinking it in the caller's transaction won't stick when
            # that transaction rolls back — exactly the failed-push path.
            # Revoke it in its own cursor so it sticks regardless, and so the
            # fresh transaction can actually SEE the committed row.
            try:
                with self.env.registry.cursor() as rev_cr:
                    rev_env = api.Environment(rev_cr, self.env.uid, self.env.context)
                    row = rev_env["res.users.apikeys"].sudo().browse(key_id)
                    if row.exists():
                        row.unlink()
            except Exception as exc:
                _logger.warning("[orc] failed to revoke committed key %s: %s", key_id, exc)
            return
        try:
            row = self.env["res.users.apikeys"].sudo().browse(key_id)
            if row.exists():
                row.unlink()
        except Exception as exc:
            _logger.warning("[orc] failed to revoke key %s: %s", key_id, exc)

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

        The new key is committed in its own cursor (see
        ``_orc_generate_api_key``) so AI Workplace's setup-key probe can see
        it; on any exception between (1) and (3) we revoke it durably
        (``commit=True``), with the orphan-cleanup cron as the backstop.
        """
        for user in self:
            if not user.active:
                continue
            client = self.env["orc.client"]

            # 1. New key first (old still valid). `_orc_generate_api_key`
            # returns the new key's id (int) — captured inside the generating
            # cursor, because the outer snapshot can't see the committed row.
            new_raw_key, new_key_id = user._orc_generate_api_key()
            old_key_row = user.orc_api_key_id

            try:
                # 2. Ensure the org_user exists in AI Workplace.  Two-
                # namespace model (plan §1 + §9): the addon only ever
                # creates org_users (members); admin promotion is a
                # platform_user concern handled by the dashboard's
                # invite flow.  No `role` parameter is sent — the
                # server defaults to member and rejects role=admin on
                # this path.
                #
                # `odoo_login` is the per-org identity key on the
                # gateway side.  We send the qualified
                # `_orc_effective_email` so bare logins (e.g. "admin")
                # don't collide across Odoo instances when the user
                # shows up in the dashboard.  The optional `email`
                # field carries the same value as display metadata.
                # provision_user is idempotent on (pinned_org_id,
                # odoo_login), so re-calls on every cron tick are
                # cheap.
                eff_email = user._orc_effective_email()
                orc_uid = client.provision_user(
                    odoo_login=eff_email,
                    name=user.name or user.login,
                    email=eff_email,
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
                # The new key was committed in its own cursor (so AI Workplace
                # could probe it), so it survives this transaction's rollback —
                # revoke it durably rather than leaking a live key.
                user._orc_revoke_key(new_key_id, commit=True)
                raise

            # 4. Revoke old key (if any). Best-effort — its presence
            #    won't leak access now that AI Workplace has the new one, but we
            #    remove it to cap blast radius.
            if old_key_row and old_key_row.id != new_key_id:
                user._orc_revoke_key(old_key_row)

            now = fields.Datetime.now()
            user.sudo().write({
                "orc_api_key_id": new_key_id,
                "orc_provisioned_at": user.orc_provisioned_at or now,
                "orc_last_rotation_at": now,
                "orc_gateway_email": eff_email,
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
                client.revoke_infra_access(email=user._orc_gateway_identity())
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
        # Plan §9.3 server-side guard: a write that changes `login`
        # AND tries to keep / flip `orc_enabled=True` is rewritten to
        # clear orc_enabled.  The onchange above handles the form UX;
        # this handles scripted / XML-RPC writes that bypass onchange.
        # We mutate the incoming `vals` so the in-flight save (and
        # any downstream code that reads vals) sees the corrected
        # shape.
        if "login" in vals:
            for user in self:
                if user.login != vals["login"] and user.orc_enabled:
                    vals = {**vals, "orc_enabled": False}
                    break

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
        # Build local index keyed by gateway identity, exactly one key
        # per user. Legacy users without a stored orc_gateway_email may
        # live on the gateway under either their raw login or the
        # qualified "login@host" form; register only the alias the remote
        # actually knows. Registering both would make the same user appear
        # "in sync" under one alias and "missing" under the other, and the
        # missing branch would re-provision a duplicate qualified identity.
        local_by_email = {}
        for u in local_enabled:
            gw_id = u._orc_gateway_identity()   # orc_gateway_email or login
            if u.orc_gateway_email:
                local_by_email[gw_id] = u
                continue
            eff = u._orc_effective_email()
            if gw_id in remote_users:
                local_by_email[gw_id] = u
            elif eff in remote_users:
                local_by_email[eff] = u
            else:
                # Absent on the gateway → provision under the canonical
                # qualified form that action_orc_provision() pushes.
                local_by_email.setdefault(eff, u)

        # Direction A — local enabled, sync forward.
        for email, user in local_by_email.items():
            if email in remote_users:
                # Heal: persist the confirmed gateway email so all future
                # calls (revoke, SSO, tasks) use the stable stored value.
                if not user.orc_gateway_email:
                    user.sudo().write({"orc_gateway_email": email})
                # Validity guard: AI Workplace holding a key ROW is NOT proof
                # the key works. If our local ownership pointer is lost
                # (orc_api_key_id empty, or dangling to a GC'd row), the key AI
                # Workplace stores is one Odoo no longer has — every tool call
                # fails to authenticate. Re-provision to restore a matching
                # pair rather than stamping "in sync" over a dead key. (Cheap:
                # a local field read, no extra network. Self-heals users left
                # broken by the rotation-pointer bug.)
                owned = user.orc_api_key_id
                if owned and owned.exists():
                    user._orc_stamp_sync("ok", "in sync")
                    continue
                try:
                    user.action_orc_provision()
                    user._orc_stamp_sync("ok", "healed: local key missing, re-provisioned")
                except Exception as exc:
                    _logger.warning(
                        "[orc] reconcile heal (lost local key) failed for %s: %s",
                        user.login, exc,
                    )
                    user._orc_stamp_sync("error", f"heal failed: {exc}")
                    self.env["orc.audit.log"].sudo().create({
                        "user_id": user.id,
                        "action": "reconcile",
                        "status": "error",
                        "error": str(exc)[:1000],
                    })
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
        # `orc_enabled=True` row.
        residual_remote = set(remote_users) - set(local_by_email)
        if residual_remote:
            self._reconcile_revoke_residual(client, residual_remote)

    @api.model
    def _reconcile_revoke_residual(self, client, residual_remote):
        """Direction B of the reconcile: remote users with no locally-enabled
        counterpart. Two sub-cases:
          1. Local user exists with orc_enabled=False → deprovision.
          2. No local user at all → orphan, log only (we don't
             auto-create res.users from the remote list).
        """
        # Search previously provisioned disabled users and key by
        # gateway identity so bare logins like "admin" match their
        # qualified form "admin@hostname".
        local_disabled_provisioned = self.search([
            ("orc_enabled", "=", False),
            ("orc_user_id", "!=", False),
        ])
        disabled_by_email = {
            u._orc_gateway_identity(): u for u in local_disabled_provisioned
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
        """Two-direction orphan cleanup for the managed-key relation.

        Forward (user → key): clear ``orc_api_key_id`` pointers that
        reference a ``res.users.apikeys`` row which no longer exists.
        This can't be left to the field's ``ondelete="set null"``:
        ``res.users.apikeys`` is ``_auto=False`` so Odoo never creates
        a real DB FK for the Many2one, and Odoo core garbage-collects
        expired keys with a raw-SQL ``DELETE`` (``_gc_user_apikeys``)
        that bypasses the ORM unlink the ``set null`` rule rides on.
        A stale pointer makes every read of the user (e.g. opening the
        user form) raise ``MissingError``, so heal it nightly.

        Reverse (key → user): revoke AI Workplace-tagged api keys not
        referenced by any res.users.
        """
        # Forward direction — raw SQL, since the dangling rows are
        # invisible to the ORM (the referenced key is already gone).
        self.env.cr.execute(
            """
            UPDATE res_users u SET orc_api_key_id = NULL
            WHERE orc_api_key_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM res_users_apikeys k WHERE k.id = u.orc_api_key_id
              )
            """
        )
        if self.env.cr.rowcount:
            _logger.info(
                "[orc] cleared %s dangling orc_api_key_id pointer(s)",
                self.env.cr.rowcount,
            )
        # Drop any cached stale Many2one values read before the UPDATE.
        self.env.invalidate_all()

        # Reverse direction — key rows no user points at.
        #
        # Grace window: never reap a key younger than an hour. With the
        # ownership pointer now set atomically at generation, a fresh
        # unreferenced managed key shouldn't occur — but this stops the reaper
        # from ever deleting a key while a provision is briefly in flight (the
        # failure mode that silently broke rotations). Older unreferenced keys
        # are genuine orphans and still get cleaned up.
        grace_cutoff = fields.Datetime.subtract(fields.Datetime.now(), hours=1)
        keys = self.env["res.users.apikeys"].sudo().search([
            ("name", "=", ORC_KEY_NAME),
            ("create_date", "<", grace_cutoff),
        ])
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
