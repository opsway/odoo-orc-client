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
    # Deliberately an Integer and NOT a Many2one onto res.users.apikeys.
    #
    # Two production outages came from modelling this as a relation:
    #
    #  1. The key is minted in a nested cursor that commits on its own (so AI
    #     Workplace's cross-connection setup-key probe can see it — see
    #     `_orc_generate_api_key`). Odoo runs cursors at REPEATABLE READ, so
    #     the transaction doing the provisioning cannot resolve the id it was
    #     just handed. `web_save` reads every form field back in that same
    #     transaction, so rendering the relation raised MissingError and rolled
    #     the whole save back — no UI provisioning was possible at all.
    #  2. `res.users.apikeys` is `_auto=False` and core GCs expired keys with a
    #     raw-SQL DELETE (`_gc_user_apikeys`), so `ondelete="set null"` — which
    #     only ever rides on the ORM unlink — left the pointer dangling and
    #     broke every read of the user form.
    #
    # Both are the same defect: an ORM relation onto a table with no real FK
    # whose rows can vanish or be invisible. This field is pure bookkeeping
    # (which key do we own?), so it stores the bare id and nothing ever reads
    # through it. `_orc_key_exists` is the one place that resolves it, and it
    # tolerates a missing row. 0 means "we own no key".
    orc_api_key_ref = fields.Integer(
        string="Managed API key",
        readonly=True,
        copy=False,
        help=(
            "Internal id of the auto-managed res.users.apikeys row this user "
            "owns. Stored as a plain id, not a relation: the key row may be "
            "committed by another transaction or garbage-collected by core, "
            "and neither must be able to break the user form."
        ),
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
        # No "drift" state: every drift the reconcile detects is remediated in
        # the same pass (Direction A re-provisions, Direction B revokes) and
        # then stamped ok/error, so it was never reachable — the value and its
        # badge decoration only ever suggested a state the code cannot produce.
        # Drift IS recorded, on `orc.audit.log.status`.
        selection=[
            ("ok", "OK"),
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
    # AI Workplace is the sole authority for this flag; this column is a
    # CACHE, not a second source of truth. Two rules keep it that way:
    #
    #   - Refresh is PULL-only. `_cron_orc_reconcile` copies the remote
    #     value in; nothing ever re-asserts the local one outbound. That
    #     periodic re-assertion is what used to clobber dashboard-set
    #     state, and it is the reason the old Odoo-side read-only gate was
    #     removed in 18.0.1.6.0.
    #   - Writes are WRITE-THROUGH. Editing the field calls AI Workplace
    #     first (as the acting admin, whose own permission is evaluated
    #     there) and only persists locally once that succeeds — see
    #     `write()`. A failure raises, so the form rolls back rather than
    #     leaving Odoo claiming a posture the platform never accepted.
    #
    # So the value is editable here, but Odoo never *decides* it. Only as
    # fresh as `orc_last_sync_at` between ticks. Note enforcement itself
    # trails a flip by up to the platform's credential-cache TTL (~60s),
    # exactly as it does when flipped from the dashboard.
    orc_read_only = fields.Boolean(
        string="AI tools are read-only",
        copy=False,
        help=(
            "Whether this user's AI tools may only READ from Odoo. Changing it "
            "here applies the change in AI Workplace immediately, under your "
            "own permissions — you must be an organization admin there, and "
            "the change is refused if you are not. AI Workplace remains the "
            "source of truth; this field reflects its state as of 'Last "
            "synced at'. It can take about a minute for a running agent to "
            "pick up the change."
        ),
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
        try:
            # Own cursor → commits on clean exit, so the row is visible to AI
            # Workplace's cross-connection probe before the caller pushes it.
            with self.env.registry.cursor() as key_cr:
                key_env = api.Environment(key_cr, self.env.uid, self.env.context)
                # No expiry passed: 17.0's `res.users.apikeys` has neither the
                # `expiration_date` column nor the `_generate` parameter (both
                # arrived in 18.0). Nothing is lost — key lifetime is enforced
                # entirely on the Odoo side by `_cron_orc_rotate_keys`, which
                # regenerates on `orc_last_rotation_at` age against
                # `orc.rotation_days`. The column was only ever a second,
                # redundant expiry that fired at the same cadence.
                raw_key = (
                    key_env["res.users.apikeys"]
                    .with_user(self.id)
                    .sudo()
                    ._generate(scope=None, name=ORC_KEY_NAME)
                )
                # Capture the id HERE, inside the generating cursor, where the
                # row is unambiguously visible. Re-searching from the *outer*
                # transaction (as this used to) silently returns EMPTY: Odoo
                # runs cursors at REPEATABLE READ, so the caller's snapshot —
                # opened before this nested cursor committed — cannot see the
                # new row. That empty result made the caller store
                # `orc_api_key_ref = 0`; the nightly orphan-cleanup cron then
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
        # is a plain int the caller stores directly into orc_api_key_ref
        # (res.users.apikeys is _auto=False → no FK existence check on write).
        return raw_key, new_key_id

    def _orc_revoke_key(self, key_id, commit=False):
        """Revoke an Odoo API key by bare id.

        Always an id, never a recordset: the freshly generated key is committed
        in its own cursor and is therefore NOT visible under the caller's
        REPEATABLE READ snapshot, so browsing it here can legitimately resolve
        to nothing. Each branch below gates on its own ``.exists()`` in the
        cursor that will do the unlink, rather than trusting the caller's.
        """
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

    def _orc_key_exists(self, key_id):
        """True when ``key_id`` resolves to a live api-key row *in this
        transaction*. Uses ``exists()``, which returns an empty recordset for a
        missing row instead of raising — the whole reason the ownership pointer
        is a bare id. Note the "in this transaction" caveat is load-bearing: a
        key committed by another cursor after this transaction's snapshot opened
        reads as missing here, which is correct for every caller (all of them
        run in a fresh transaction: crons and the reconcile pass).
        """
        if not key_id:
            return False
        return bool(self.env["res.users.apikeys"].sudo().browse(key_id).exists())

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

    def _orc_mirror_read_only(self, remote):
        """Mirror AI Workplace's read-only posture onto this user.

        `remote` is one entry of the `/api/addon/infrastructure-users`
        response. This is the PULL direction and stays one-directional: the
        refresh never posts anything back. Authoring is a separate path
        (`write()` → `_orc_push_read_only`), reached only by user action.

        An older gateway omits `read_only` entirely. Treat a missing key
        as "unknown" and leave the stored value alone rather than writing
        False — writing would claim the user has write access, which is
        the wrong way to be wrong, and would flap the field on every tick
        against a gateway that hasn't shipped the field yet.
        """
        if not isinstance(remote, dict) or "read_only" not in remote:
            return False
        value = bool(remote.get("read_only"))
        if self.orc_read_only == value:
            return False
        # `orc_readonly_internal` marks this as the PULL direction. Without
        # it, `write()` below would call AI Workplace right back — turning
        # the refresh into a re-assertion and rebuilding the clobber loop
        # this whole design exists to avoid, one hop further out.
        self.sudo().with_context(orc_readonly_internal=True).write(
            {"orc_read_only": value}
        )
        return True

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
            old_key_id = user.orc_api_key_ref

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
            if old_key_id and old_key_id != new_key_id:
                user._orc_revoke_key(old_key_id)

            now = fields.Datetime.now()
            user.sudo().write({
                "orc_api_key_ref": new_key_id,
                "orc_provisioned_at": user.orc_provisioned_at or now,
                "orc_last_rotation_at": now,
                "orc_gateway_email": eff_email,
            })

            self.env["orc.audit.log"].sudo().create({
                "user_id": user.id,
                "action": "provision" if not old_key_id else "rotate",
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

            user._orc_revoke_key(user.orc_api_key_ref)
            # Same internal marker as the mirror refresh: clearing a cache
            # entry for a user who no longer has access is bookkeeping, and
            # must not post a posture change for a credential we just
            # revoked (AI Workplace would 404 the target, or worse succeed
            # against a re-provisioned one).
            user.sudo().with_context(orc_readonly_internal=True).write({
                "orc_enabled": False,
                "orc_api_key_ref": 0,
                "orc_last_rotation_at": False,
                # Not a breadcrumb — a posture only means something while
                # access exists. Leaving it set would show "read-only" next
                # to a user who has no AI access at all.
                "orc_read_only": False,
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
    # to record their bookkeeping (orc_api_key_ref, orc_last_*); without
    # a marker the write override below would re-trigger them and
    # recurse forever. Anything tagged with this context bypasses the
    # provisioning logic and just persists the row.
    _ORC_INFLIGHT_CTX = "orc_provisioning_inflight"

    def _orc_push_read_only(self, value):
        """Apply a read-only flip in AI Workplace, as the acting admin.

        Called from `write()` BEFORE the local value is stored, so a
        refusal (not an org admin there, unreachable, unknown target)
        raises and the whole save rolls back. Odoo must never end up
        claiming a posture the platform did not accept.

        The acting identity is `_orc_gateway_identity()` of the CURRENT
        user — the same derivation every other user-scoped call uses, and
        the only string guaranteed to match whichever shape AI Workplace
        already stores for them. A consequence worth knowing: the admin
        doing this must themselves be provisioned there; a purely local
        Odoo admin is refused, which is correct — the permission lives in
        AI Workplace, not in Odoo's groups.
        """
        client = self.env["orc.client"]
        acting = self.env.user._orc_gateway_identity()
        for user in self:
            client.set_read_only(
                email=user._orc_gateway_identity(),
                read_only=value,
                acting_user=acting,
            )

    @api.model_create_multi
    def create(self, vals_list):
        """Creation does not pass through `write()`.

        So a posture requested at creation would be stored locally and never
        sent — and then silently reverted by the next mirror refresh, which
        is the worst of both worlds. Strip it, create, and re-apply it
        through the ordinary intent path once the user actually holds a
        credential.

        Note `create()` does not provision either — only `write()` runs the
        enable cascade — so even `orc_enabled=True` at creation is honoured
        eventually rather than immediately (reconcile's Direction A picks it
        up on the next hourly tick). There is therefore no credential to
        author a posture against at this point, whatever the flags say, and
        the request is dropped rather than stored: storing it would leave
        Odoo claiming a posture AI Workplace never heard of, which the mirror
        refresh would then silently revert.

        Dropped rather than raised, because failing the creation of an Odoo
        user over this would be disproportionate. The form also hides the
        control on an unsaved record, so this path is reached mainly by
        scripted / XML-RPC creates, where the log line is the signal.
        """
        wanted = [bool(v.get("orc_read_only")) for v in vals_list]
        if any(wanted):
            vals_list = [
                {k: val for k, val in v.items() if k != "orc_read_only"}
                for v in vals_list
            ]
        users = super().create(vals_list)
        for user, want in zip(users, wanted):
            if not want:
                continue
            if user.orc_enabled and user.orc_api_key_ref:
                user.write({"orc_read_only": True})
            else:
                _logger.info(
                    "[orc] ignoring orc_read_only=True at creation for %s: "
                    "no AI Workplace credential yet — set it once access is "
                    "enabled",
                    user.login,
                )
        return users

    def _orc_apply_pushed_read_only(self, records, value):
        """Push a posture, then cache it — in that order, or not at all.

        Storing before (or without) a successful push is what would let Odoo
        claim a posture AI Workplace never accepted. A raise from the push
        propagates, rolling the whole save back.
        """
        if not records:
            return
        records._orc_push_read_only(value)
        records.sudo().with_context(orc_readonly_internal=True).write(
            {"orc_read_only": value}
        )

    def write(self, vals):
        # Plan §9.3 server-side guard: a write that changes `login`
        # AND tries to keep / flip `orc_enabled=True` is rewritten to
        # clear orc_enabled.  The onchange above handles the form UX;
        # this handles scripted / XML-RPC writes that bypass onchange.
        # We continue the save from a corrected copy of `vals`, so the
        # in-flight write (and the cascade below, which reads
        # `flip_to` off it) sees the corrected shape.  The caller's own
        # dict is left alone.
        forced_off = False
        if "login" in vals:
            for user in self:
                if user.login == vals["login"]:
                    continue
                # Test the EFFECTIVE post-write value, not the stored one: a
                # single write can both rename and flip orc_enabled on, and
                # enrolling under a login the admin never consciously
                # enrolled is exactly what this guard exists to prevent.  For
                # a previously-deprovisioned user the `orc_user_id`
                # breadcrumb still points at the OLD gateway identity, so
                # provisioning under the new login would mint a second one
                # and orphan the first.
                if vals.get("orc_enabled", user.orc_enabled):
                    vals = {**vals, "orc_enabled": False}
                    forced_off = True
                    break

        # Write-through for the read-only posture.
        #
        # Decided HERE — after the login guard, which may have just forced
        # `orc_enabled` off (a rename deprovisions, so a posture pushed for
        # that identity would target a credential being revoked) — and
        # PERFORMED after the enable/disable cascade below, once the
        # credential a push needs actually exists. Ticking access and
        # read-only in one save is an ordinary thing to do, and pushing
        # first would make AI Workplace answer 404 and abort the save.
        #
        # The value is REMOVED from `vals`, so `super().write()` never
        # stores it. Odoo must never hold a posture that was not applied
        # remotely: storing one for a user we decline to push for (disabled,
        # or being disabled) would show "read-only" next to a credential
        # that is still writable, and the next enable would not re-push it —
        # it would just look correct until the mirror refresh quietly
        # reverted it. So the local write happens only after a successful
        # push, through the internal (pull-direction) path.
        #
        # Selection must happen before `super().write()`: afterwards every
        # record would already equal the target and "actually changing"
        # would match nothing.
        #
        # Skipped when `orc_readonly_internal` (the pull direction: mirror
        # refresh and deprovision clear) or when inside the cascade's own
        # in-flight writes.
        #
        # On a multi-record write the platform applies each user in turn; if
        # one fails, Odoo rolls back locally while the earlier ones stand.
        # That is the safe direction — the cache diverges for at most one
        # cron tick, then converges TOWARD AI Workplace, the authority. No
        # compensation logic, on purpose.
        push_target = None
        push_records = self.browse()
        if (
            "orc_read_only" in vals
            and not self.env.context.get("orc_readonly_internal")
            and not self.env.context.get(self._ORC_INFLIGHT_CTX)
        ):
            push_target = bool(vals["orc_read_only"])
            push_records = self.filtered(
                lambda u: u.orc_read_only != push_target
                and vals.get("orc_enabled", u.orc_enabled)
            )
            vals = {k: v for k, v in vals.items() if k != "orc_read_only"}

        if (
            "orc_enabled" not in vals
            or self.env.context.get(self._ORC_INFLIGHT_CTX)
        ):
            res = super().write(vals)
            self._orc_apply_pushed_read_only(push_records, push_target)
            return res

        flip_to = vals["orc_enabled"]
        # Mark the cascade so action_orc_provision / action_orc_deprovision's
        # internal writes can persist orc_api_key_ref, orc_last_rotation_at,
        # and (when deprovisioning) orc_enabled itself without re-entering
        # this hook.
        self_inflight = self.with_context(**{self._ORC_INFLIGHT_CTX: True})
        res = super(ResUsers, self_inflight).write(vals)
        # Users this write provisions from scratch. Needed to undo the grant
        # if the posture push below fails — see the handler there.
        provisioned_now = []
        for user in self_inflight:
            # Re-provision fires when `orc_enabled` flips true AND
            # there's no live AI Workplace-managed API key — covers both the
            # "never enrolled" case (orc_user_id is None) and the
            # "previously unchecked, now re-ticked" case (orc_user_id
            # survives as a breadcrumb but orc_api_key_ref was cleared
            # on deprovision).
            if flip_to and not user.orc_api_key_ref:
                user.action_orc_provision()
                provisioned_now.append(user)
                user._orc_stamp_sync("ok", "provisioned on save")
            elif not flip_to and user.orc_user_id:
                if not forced_off:
                    user.action_orc_deprovision()
                    user._orc_stamp_sync("ok", "deprovisioned on save")
                    continue
                # `orc_enabled: False` was injected by the login guard above,
                # not asked for by the caller: this is a RENAME, and
                # `action_orc_deprovision` makes a synchronous
                # `revoke_infra_access` call.  Letting that raise would abort
                # the rename over an unrelated AI Workplace outage — and leave
                # the user enrolled anyway, so the admin gets neither the
                # rename nor the revoke.  Swallow it instead: `orc_enabled` is
                # already False locally, and reconcile's Direction B (local
                # disabled + `orc_user_id` set → revoke) retries the remote
                # side on the next hourly tick.  Revocation is therefore
                # eventually-consistent on this path, which is strictly better
                # than the rename failing outright.
                try:
                    user.action_orc_deprovision()
                    user._orc_stamp_sync("ok", "deprovisioned on save")
                except Exception as exc:
                    _logger.warning(
                        "[orc] deprovision on login change failed for %s: %s",
                        user.login, exc,
                    )
                    user._orc_stamp_sync(
                        "error", f"login changed; deprovision failed: {exc}",
                    )
        # After provisioning: the credential the push needs now exists.
        try:
            self._orc_apply_pushed_read_only(push_records, push_target)
        except Exception:
            # Compensate. Provisioning has already granted remote access and
            # committed the Odoo API key in its OWN cursor (see
            # `_orc_generate_api_key`), so neither is undone by the rollback
            # this raise is about to trigger. That would leave live access
            # the admin's failed save says was never granted — and leave it
            # READ-WRITE, the precise thing they were trying to restrict.
            #
            # Reconcile cannot mop it up either: the rollback discards the
            # `orc_user_id` breadcrumb that Direction B (local disabled +
            # orc_user_id set → revoke) keys off, so the remote identity
            # reads as an unreferenced orphan and survives until key
            # expiry.
            #
            # Only users provisioned by THIS write are undone — an
            # already-enrolled user's access is not ours to revoke over a
            # failed posture change. Revoke failures are logged, not raised:
            # the original error is the one the admin needs to see.
            for user in provisioned_now:
                # Capture the key id first: `action_orc_deprovision` clears
                # `orc_api_key_ref`, and we need it after.
                key_ref = user.orc_api_key_ref
                try:
                    user.action_orc_deprovision()
                except Exception as exc:
                    _logger.warning(
                        "[orc] could not undo provisioning for %s after a failed "
                        "posture update: %s", user.login, exc,
                    )
                # `action_orc_deprovision`'s own key unlink runs in THIS
                # transaction, so the raise below would roll it back and leave
                # the separately-committed key row alive and unreferenced. Redo
                # it in its own cursor — the `commit=True` branch exists for
                # this exact path, and says so.
                user._orc_revoke_key(key_ref, commit=True)
            raise
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
                # Refresh the read-only mirror while we hold a definitive
                # remote answer. Done before the key-validity branch below
                # so it lands on the healed path too — that branch
                # re-provisions and `continue`s, and the posture we just
                # read is still the truth either way.
                user._orc_mirror_read_only(remote_users[email])
                # Validity guard: AI Workplace holding a key ROW is NOT proof
                # the key works. If our local ownership pointer is lost
                # (orc_api_key_ref empty, or dangling to a GC'd row), the key AI
                # Workplace stores is one Odoo no longer has — every tool call
                # fails to authenticate. Re-provision to restore a matching
                # pair rather than stamping "in sync" over a dead key. (Cheap:
                # a local field read, no extra network. Self-heals users left
                # broken by the rotation-pointer bug.)
                if user._orc_key_exists(user.orc_api_key_ref):
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
            try:
                self._reconcile_revoke_residual(client, residual_remote)
            except Exception:
                # Direction A's per-user stamps are already written in this
                # transaction. A Direction-B blow-up must not roll them back —
                # that turned a single bad remote entry into a total no-op for
                # the whole tick, invisibly.
                _logger.exception("[orc] reconcile residual pass failed")

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
        # Per-email isolation, matching Direction A's per-user try/except: one
        # bad remote entry must not abort the rest of the pass. Only the revoke
        # used to be guarded, so anything raised outside it (e.g. the orphan
        # audit row) escaped all the way out of `_cron_orc_reconcile` and rolled
        # the whole tick back — remaining residual users never revoked, every
        # stamp from the pass discarded.
        for email in residual_remote:
            user = disabled_by_email.get(email)
            try:
                if user is None:
                    self.env["orc.audit.log"].sudo().create({
                        "action": "orphan_remote_user",
                        "status": "drift",
                        "error": f"no local res.users for {email}"[:1000],
                    })
                    continue
                client.revoke_infra_access(email=email)
                user._orc_stamp_sync("ok", "deprovisioned from AI Workplace")
            except Exception as exc:
                _logger.warning(
                    "[orc] reconcile deprovision failed for %s: %s",
                    email, exc,
                )
                if user is not None:
                    user._orc_stamp_sync("error", f"deprovision failed: {exc}")
                try:
                    self.env["orc.audit.log"].sudo().create({
                        "user_id": user.id if user is not None else False,
                        "action": "reconcile",
                        "status": "error",
                        "error": str(exc)[:1000],
                    })
                except Exception:
                    # Never let the failure bookkeeping itself break the loop.
                    _logger.exception("[orc] could not record reconcile failure")

    @api.model
    def _cron_orc_orphan_cleanup(self):
        """Two-direction orphan cleanup for the managed-key relation.

        Forward (user → key): clear ``orc_api_key_ref`` pointers that
        reference a ``res.users.apikeys`` row which no longer exists.
        Odoo core garbage-collects expired keys with a raw-SQL ``DELETE``
        (``_gc_user_apikeys``), and ``res.users.apikeys`` is ``_auto=False``
        so there is no real FK to cascade the deletion into this column
        either — the pointer is simply left dangling.

        Since the pointer is a bare id (see the field comment) a dangling
        value is now harmless rather than fatal: nothing reads through it,
        and ``_orc_key_exists`` treats it as "we own no key", so reconcile
        re-provisions. This pass is bookkeeping hygiene — it keeps the
        column honest so the reverse direction below can trust it.

        Reverse (key → user): revoke AI Workplace-tagged api keys not
        referenced by any res.users.
        """
        # Forward direction — plain SQL: this is a bulk predicate over a
        # column, and the rows it corrects are exactly the ones whose
        # referenced key is already gone.
        self.env.cr.execute(
            """
            UPDATE res_users u SET orc_api_key_ref = 0
            WHERE orc_api_key_ref IS NOT NULL
              AND orc_api_key_ref != 0
              AND NOT EXISTS (
                  SELECT 1 FROM res_users_apikeys k WHERE k.id = u.orc_api_key_ref
              )
            """
        )
        if self.env.cr.rowcount:
            _logger.info(
                "[orc] cleared %s dangling orc_api_key_ref pointer(s)",
                self.env.cr.rowcount,
            )
        # Drop any cached values read before the UPDATE.
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
        # `> 0`, not `!= 0`: the column is nullable (rows predating the
        # 18.0.1.17.0 migration, and any user never provisioned), and Odoo
        # renders `!=` on an integer as "value differs OR IS NULL" — which
        # would pull every unprovisioned user into the scan for nothing.
        referenced_ids = set(
            self.search([("orc_api_key_ref", ">", 0)]).mapped("orc_api_key_ref")
        )
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
