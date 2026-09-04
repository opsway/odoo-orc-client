from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase

from .common import patch_orc_client, share_test_cursor

# The AI Workplace page of the user form, as the web client asks for it on
# save. Kept faithful to views/res_users_views.xml: every field there is in
# the read-back spec, including ones hidden behind a *dynamic* `invisible`
# modifier (the client evaluates those itself, so they stay in the spec).
# The AI Workplace page's fields. v15's form client loads a record with
# `read(fields)` and saves with `write(vals)` followed by a read-back of the
# same fields, so this is a flat list rather than the nested spec `web_read` /
# `web_save` take on 17.0+.
USER_FORM_FIELDS = [
    "orc_enabled",
    "orc_user_id",
    "orc_provisioned_at",
    "orc_last_rotation_at",
    "orc_last_sync_at",
    "orc_last_sync_status",
    "orc_last_sync_message",
    "orc_api_key_ref",
]


class TestOrcProvisioning(TransactionCase):
    """
    Exercises the Odoo-side branches of action_orc_provision /
    action_orc_deprovision. All ORC HTTP calls are mocked — the tests
    verify the lifecycle (key create / key revoke / row updates /
    audit log) without hitting the network.
    """

    def setUp(self):
        super().setUp()
        share_test_cursor(self)
        self.user = self.env["res.users"].create({
            "name": "Alice Example",
            "login": "alice@acme.test",
        })
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("orc.endpoint_url", "https://orc.test")
        icp.set_param("orc.org_token", "orc_test_token")
        icp.set_param("orc.infrastructure_id", "11111111-1111-1111-1111-111111111111")

    def _patch_client(self, **overrides):
        defaults = {
            "provision_user": lambda **kw: "orc-uid-1",
            "push_odoo_key": lambda **kw: None,
            "revoke_infra_access": lambda **kw: None,
            # Default: the gateway knows about nobody. Reconcile tests override
            # this to place a user in the remote directory.
            "list_users": lambda **kw: {"users": []},
        }
        defaults.update(overrides)
        return patch_orc_client(
            self.env,
            provision_user=defaults["provision_user"],
            push_odoo_key=defaults["push_odoo_key"],
            revoke_infra_access=defaults["revoke_infra_access"],
            list_users=defaults["list_users"],
        )

    def test_provision_creates_key_and_records_audit(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        self.assertEqual(self.user.orc_user_id, "orc-uid-1")
        self.assertTrue(self.user.orc_api_key_ref)
        key = self.env["res.users.apikeys"].sudo().browse(self.user.orc_api_key_ref)
        self.assertEqual(key.name, "AI Workplace (auto-managed)")
        self.assertTrue(self.user.orc_provisioned_at)
        self.assertTrue(self.user.orc_last_rotation_at)
        log = self.env["orc.audit.log"].search([("user_id", "=", self.user.id)], limit=1)
        self.assertEqual(log.action, "provision")
        self.assertEqual(log.status, "ok")

    def test_provision_on_write_stamps_last_sync(self):
        """Flipping orc_enabled=True via write() must stamp the
        last-sync triple so the form renders ✓ + a recent timestamp
        immediately, without waiting for the hourly cron."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertTrue(self.user.orc_last_sync_at)
        # Exact string, not `assertIn("provisioned", ...)`: the loose form is
        # also satisfied by "deprovisioned on save" and "re-provisioned to AI
        # Workplace", so it could not tell provision from its opposite.
        self.assertEqual(self.user.orc_last_sync_message, "provisioned on save")

    def test_push_odoo_key_payload_does_not_include_access_level(self):
        # INT-842: per-user access axis was dropped. push_odoo_key
        # must no longer ship `access_level` to ORC.
        captured = {}

        def fake_push(**kw):
            captured.update(kw)

        with self._patch_client(push_odoo_key=fake_push):
            self.user.orc_enabled = True
        self.assertNotIn("access_level", captured)
        self.assertIn("api_key", captured)
        self.assertEqual(captured.get("email"), self.user.login)

    def test_provision_rollback_on_push_key_failure(self):
        def fail_push(**kw):
            raise UserError("boom")

        with self._patch_client(push_odoo_key=fail_push):
            with self.assertRaises(UserError):
                self.user.orc_enabled = True

        # Rollback: no ORC uid, no key row persists.
        self.user.invalidate_cache(ids=self.user.ids)
        self.assertFalse(self.user.orc_user_id)
        self.assertFalse(self.user.orc_api_key_ref)

    def test_deprovision_revokes_this_infra_only_and_keeps_breadcrumb(self):
        """Per A₁: unticking `orc_enabled` is per-infra revoke.

        The local ORC-managed Odoo key row is deleted, the HTTP call
        to ORC is made with `X-Acting-User` so ORC can drop the
        matching `user_odoo_keys` row + `infrastructure.member`
        relation, and the Odoo-side tracking is cleared EXCEPT for
        `orc_user_id` (breadcrumb so re-ticking re-enrols the same
        ORC identity).
        """
        revoke_calls: list[dict] = []

        def capture_revoke(**kw):
            revoke_calls.append(kw)

        with self._patch_client():
            self.user.orc_enabled = True
        self.assertTrue(self.user.orc_user_id)
        orc_uid = self.user.orc_user_id
        key_id = self.user.orc_api_key_ref

        with self._patch_client(revoke_infra_access=capture_revoke):
            self.user.orc_enabled = False

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertFalse(self.user.orc_enabled)
        # Breadcrumb retained.
        self.assertEqual(self.user.orc_user_id, orc_uid)
        # Managed key row on Odoo side is gone.
        self.assertFalse(self.user.orc_api_key_ref)
        self.assertFalse(self.env["res.users.apikeys"].search([("id", "=", key_id)]))
        # ORC was told to revoke using the gateway identity (orc_gateway_email
        # stored at provision time). For alice@acme.test it equals login.
        self.assertEqual(revoke_calls, [{"email": self.user._orc_gateway_identity()}])

    def test_retick_after_deprovision_reprovisions(self):
        """A₁ round-trip: uncheck then re-check → fresh provisioning
        runs against the kept breadcrumb `orc_user_id`.
        """
        with self._patch_client():
            self.user.orc_enabled = True
        orc_uid = self.user.orc_user_id

        with self._patch_client():
            self.user.orc_enabled = False

        provision_calls: list[dict] = []

        def capture_provision(**kw):
            provision_calls.append(kw)
            return orc_uid  # ORC side is idempotent; returns same id

        with self._patch_client(provision_user=capture_provision):
            self.user.orc_enabled = True

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertTrue(self.user.orc_api_key_ref)  # fresh key pushed
        # provision_user was actually called despite the breadcrumb
        # being present (write-hook keys off `orc_api_key_ref`, not
        # `orc_user_id`, to catch re-enrolment).
        self.assertEqual(len(provision_calls), 1)

    def test_dangling_apikey_pointer_does_not_break_the_user_form(self):
        """A managed key hard-deleted out-of-band must not make the user
        unreadable.

        Odoo core GCs expired api keys with a raw-SQL DELETE
        (`_gc_user_apikeys`). Because `res.users.apikeys` is `_auto=False`
        there is no real DB FK either, so nothing cascades into the ownership
        pointer and it is left referencing a row that no longer exists. While
        this was a Many2one that state raised MissingError on every read of
        the user — the form would not even open, and healing it needed a cron
        that a neutralized (staging) database has switched off. As a bare id
        the dangling value is inert.
        """
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        key_id = self.user.orc_api_key_ref
        self.assertTrue(key_id)

        # Simulate core's raw-SQL GC of the expired key.
        self.env.cr.execute(
            "DELETE FROM res_users_apikeys WHERE id = %s", (key_id,)
        )
        self.env.cache.invalidate()

        # The form reads end-to-end while the pointer is still dangling —
        # no cron, no migration, nothing to heal first.
        self.user.read(USER_FORM_FIELDS)
        self.assertEqual(self.user.orc_api_key_ref, key_id)
        # ...and the pointer reads as "we own no key", so reconcile treats it
        # as drift and re-provisions rather than stamping "in sync".
        self.assertFalse(self.user._orc_key_exists(self.user.orc_api_key_ref))

        # Nightly cleanup still tidies the column up.
        self.env["res.users"]._cron_orc_orphan_cleanup()
        self.user.invalidate_cache(ids=self.user.ids)
        self.assertFalse(self.user.orc_api_key_ref)

    # -- rotation key-pointer / reconcile-validity regression -------------------

    def test_reconcile_reprovisions_when_local_key_pointer_lost(self):
        """Regression for the rotation data-loss bug.

        A rotation once left `orc_api_key_ref` empty (the outer-transaction
        re-read couldn't see the key committed in the nested cursor) while the
        gateway still held the pushed key. The orphan reaper then deleted the
        Odoo key, so the gateway's key could no longer authenticate — yet
        reconcile kept stamping "in sync" because the gateway still listed a
        key ROW for the user.

        Reconcile must now treat "remote key present BUT local pointer lost" as
        drift and re-provision, restoring a matching Odoo↔gateway pair.
        """
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        email = self.user._orc_gateway_identity()

        # Simulate the drift: gateway still lists the user, local pointer gone.
        self.user.sudo().write({"orc_api_key_ref": 0})

        provision_calls: list[dict] = []

        def capture_provision(**kw):
            provision_calls.append(kw)
            return self.user.orc_user_id or "orc-uid-1"

        with self._patch_client(
            provision_user=capture_provision,
            list_users=lambda **kw: {"users": [{"email": email}]},
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertEqual(len(provision_calls), 1,
                         "lost pointer must trigger re-provision, not 'in sync'")
        self.assertTrue(self.user.orc_api_key_ref, "ownership pointer restored")
        self.assertIn("healed", (self.user.orc_last_sync_message or "").lower())

    def test_reconcile_stays_in_sync_when_pointer_is_valid(self):
        """The validity guard must NOT re-provision a healthy user — a present,
        existing local key + a remote key row is genuinely 'in sync'."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        email = self.user._orc_gateway_identity()

        provision_calls: list[dict] = []

        def capture_provision(**kw):
            provision_calls.append(kw)
            return "orc-uid-1"

        with self._patch_client(
            provision_user=capture_provision,
            list_users=lambda **kw: {"users": [{"email": email}]},
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertEqual(provision_calls, [],
                         "valid pointer → in sync, must not re-provision")
        self.assertEqual(self.user.orc_last_sync_message, "in sync")

    def test_orphan_reaper_respects_grace_window(self):
        """A freshly-created managed key must survive the orphan reaper even if
        momentarily unreferenced — that race is what silently deleted rotated
        keys. Only keys older than the grace window are genuine orphans."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        key_id = self.user.orc_api_key_ref
        self.assertTrue(key_id)

        # Make it unreferenced (the lost-pointer state).
        self.user.sudo().write({"orc_api_key_ref": 0})

        # Fresh (create_date = now) → protected by the grace window.
        self.env["res.users"]._cron_orc_orphan_cleanup()
        self.assertTrue(
            self.env["res.users.apikeys"].browse(key_id).exists(),
            "a fresh unreferenced managed key must NOT be reaped",
        )

        # Age it past the grace window → genuine orphan → reaped.
        self.env.cr.execute(
            "UPDATE res_users_apikeys "
            "SET create_date = (now() at time zone 'UTC') - interval '2 hours' "
            "WHERE id = %s",
            (key_id,),
        )
        self.env.cache.invalidate()
        self.env["res.users"]._cron_orc_orphan_cleanup()
        self.assertFalse(
            self.env["res.users.apikeys"].browse(key_id).exists(),
            "an aged unreferenced managed key must be reaped",
        )

    # -- form-save read-back regression ----------------------------------------

    def test_form_save_provisions_when_the_new_key_row_is_unreadable(self):
        """Ticking the checkbox on the user FORM must provision and commit.

        A v15 form save is `write()` **plus a read-back in the same transaction**
        (odoo/addons/web/models/models.py). Provisioning stores the id of a key
        row created in a nested cursor that commits on its own, so the
        enclosing transaction — REPEATABLE READ, snapshot opened before that
        commit — cannot resolve the id it was just handed. Any ORM read of the
        pointed-at row therefore raised
        `MissingError: res.users.apikeys(N,)` and rolled the whole save back:
        the key was already minted and pushed to the gateway, but Odoo kept no
        record of it and `orc_enabled` never persisted, so admins could not
        provision anybody from the UI at all.

        The suite cannot reproduce the cross-transaction invisibility directly
        (`share_test_cursor` makes that nested cursor a savepoint, which is the
        reason this shipped green), so we simulate the one thing the enclosing
        transaction actually observes: a pointer id that a SELECT in this
        transaction cannot resolve. That is the same state a raw-SQL-GC'd key
        leaves behind, which is why the fix covers both.
        """
        def unresolvable_key(_self, *args, **kwargs):
            # A committed-elsewhere / already-GC'd row, from this
            # transaction's point of view: the id is unresolvable.
            return "raw-key-invisible-here", 987654321

        with self._patch_client(), patch.object(
            type(self.env["res.users"]),
            "_orc_generate_api_key",
            unresolvable_key,
        ):
            # write() + the read-back, which is what the v15 form save does
            # and the half that used to roll the whole save back.
            self.user.write({"orc_enabled": True})
            self.user.read(USER_FORM_FIELDS)

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertTrue(
            self.user.orc_enabled,
            "the form save must commit, not roll back on the read-back",
        )
        self.assertEqual(self.user.orc_user_id, "orc-uid-1")

    # -- bare-login tests -------------------------------------------------------

    def test_gateway_identity_falls_back_to_login_when_no_stored_email(self):
        """Users provisioned before orc_gateway_email was introduced have no
        stored email. _orc_gateway_identity() must return the raw login so
        revoke/SSO/tasks still reach the gateway identity they were registered under."""
        self.assertFalse(self.user.orc_gateway_email)
        self.assertEqual(self.user._orc_gateway_identity(), "alice@acme.test")

    def test_gateway_identity_uses_stored_email_after_provision(self):
        """After provisioning, orc_gateway_email is set and _orc_gateway_identity
        returns it — even if the effective email computation would differ."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("web.base.url", "https://myco.odoo.com")
        admin = self.env["res.users"].create({
            "name": "Admin User",
            "login": "admin_test_orc",
        })
        with self._patch_client():
            admin.orc_enabled = True
        admin.invalidate_cache(ids=admin.ids)
        self.assertEqual(admin.orc_gateway_email, "admin_test_orc@myco.odoo.com")
        self.assertEqual(admin._orc_gateway_identity(), "admin_test_orc@myco.odoo.com")

    def test_effective_email_with_at_sign_is_unchanged(self):
        self.assertEqual(
            self.user._orc_effective_email(),
            "alice@acme.test",
        )

    def test_effective_email_bare_login_qualified_with_hostname(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("web.base.url", "https://myco.odoo.com")
        admin = self.env["res.users"].create({
            "name": "Admin User",
            "login": "admin_test_orc",
        })
        self.assertEqual(admin._orc_effective_email(), "admin_test_orc@myco.odoo.com")

    # -- Plan §9 + task 63 — login-change guard rails --------------------------

    def test_orc_provisionable_true_for_non_empty_login(self):
        """`orc_provisionable` is the precondition for toggling
        `orc_enabled` on (the view binds `readonly` to its negation).
        Every persisted user has a non-empty login (NOT NULL at the
        DB level), so the field is True in practice — the gate is
        defensive."""
        self.assertTrue(self.user.orc_provisionable)

    def test_write_changing_login_forces_orc_enabled_off(self):
        """Plan §9.3 — the (pinned_org_id, odoo_login) gateway key
        assumes a stable login.  A scripted / XML-RPC write that
        changes `login` while orc_enabled was True must have
        orc_enabled silently flipped to False so the next reconcile
        cron doesn't silently re-provision under the new login (which
        would mint a NEW gateway-side user and leak the prior
        identity).  Admin must consciously re-enable."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        self.assertTrue(self.user.orc_enabled)
        prior_uid = self.user.orc_user_id
        self.assertTrue(prior_uid)

        # The client DOES get called: injecting orc_enabled=False sends the
        # save into the deprovision branch, which makes a synchronous
        # revoke_infra_access call.  (An earlier comment here claimed the
        # override short-circuits before touching the client — it does not,
        # and the mock below is load-bearing.)
        with self._patch_client():
            self.user.sudo().write({"login": "renamed@acme.test"})

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertFalse(self.user.orc_enabled)
        # Breadcrumb retained — re-enabling re-provisions cleanly
        # against the new login.
        self.assertEqual(self.user.orc_user_id, prior_uid)

    def test_write_login_change_survives_gateway_outage(self):
        """A login rename must not be blocked by an AI Workplace outage.

        The rename injects orc_enabled=False, which routes the save into
        action_orc_deprovision() and its synchronous revoke_infra_access call.
        If that raises, aborting the whole write would leave the admin with
        neither the rename NOR the revoke — and the user still enrolled. So
        the failure is swallowed, stamped red, and left for reconcile's
        Direction B to retry on the next tick.
        """
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)
        self.assertTrue(self.user.orc_user_id)

        def gateway_down(**kw):
            raise UserError("AI Workplace unreachable")

        with self._patch_client(revoke_infra_access=gateway_down):
            self.user.sudo().write({"login": "renamed@acme.test"})

        self.user.invalidate_cache(ids=self.user.ids)
        # The rename landed and access is off locally, despite the outage.
        self.assertEqual(self.user.login, "renamed@acme.test")
        self.assertFalse(self.user.orc_enabled)
        # ...and the failure is visible rather than silent.
        self.assertEqual(self.user.orc_last_sync_status, "error")
        self.assertIn("AI Workplace unreachable", self.user.orc_last_sync_message or "")
        # orc_user_id is retained, so reconcile Direction B picks the user up
        # (orc_enabled=False + orc_user_id set) and retries the revoke.
        self.assertTrue(self.user.orc_user_id)

    def test_write_explicit_untick_still_raises_on_gateway_outage(self):
        """The swallow above is scoped to the rename path only.

        An admin deliberately unticking the checkbox is a direct request to
        revoke; if that can't be honoured they must be told, not handed a
        silent half-success.
        """
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)

        def gateway_down(**kw):
            raise UserError("AI Workplace unreachable")

        with self._patch_client(revoke_infra_access=gateway_down):
            with self.assertRaises(UserError):
                self.user.sudo().write({"orc_enabled": False})

    def test_write_no_login_change_preserves_orc_enabled(self):
        """The login-change guard only fires on an actual change.
        Writing the same login back is a no-op for orc_enabled."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache(ids=self.user.ids)

        # Same value — guard must NOT trip.
        with self._patch_client():
            self.user.sudo().write({"login": self.user.login})

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertTrue(self.user.orc_enabled)

    def test_write_combined_login_change_plus_orc_enabled_true_is_rewritten(self):
        """The override mutates `vals` in place when the caller tries
        to flip `orc_enabled=True` and change the login in the same
        write.  Both happen on the next save: login is changed,
        orc_enabled stays False, no provisioning fires."""
        provision_calls: list[dict] = []

        def capture_provision(**kw):
            provision_calls.append(kw)
            return "orc-uid-renamed"

        with self._patch_client(provision_user=capture_provision):
            self.user.sudo().write({
                "login": "renamed@acme.test",
                "orc_enabled": True,
            })

        self.user.invalidate_cache(ids=self.user.ids)
        self.assertEqual(self.user.login, "renamed@acme.test")
        self.assertFalse(self.user.orc_enabled)
        self.assertEqual(provision_calls, [])  # never invoked

    def test_provision_bare_login_sends_qualified_email(self):
        """Bare login users must be provisioned with a qualified email so
        'admin' on two different Odoo instances does not collide in the gateway.

        Task 63 — `provision_user` now takes `odoo_login` (the per-org
        identity key on the gateway side, plan §3) instead of `email`.
        The VALUE stays the qualified email; only the field name changes.
        `email` continues to ship as optional display metadata.
        """
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("web.base.url", "https://myco.odoo.com")
        admin = self.env["res.users"].create({
            "name": "Admin User",
            "login": "admin_test_orc",
        })
        provision_calls: list[dict] = []
        push_calls: list[dict] = []

        def capture_provision(**kw):
            provision_calls.append(kw)
            return "orc-uid-admin"

        def capture_push(**kw):
            push_calls.append(kw)

        with self._patch_client(
            provision_user=capture_provision,
            push_odoo_key=capture_push,
        ):
            admin.orc_enabled = True

        self.assertEqual(len(provision_calls), 1)
        # Task 63 — odoo_login is the per-org key; same value as the
        # qualified email so the gateway-side identity is stable for
        # existing deployments.
        self.assertEqual(
            provision_calls[0]["odoo_login"], "admin_test_orc@myco.odoo.com",
        )
        # `email` ships as display metadata, optional.
        self.assertEqual(
            provision_calls[0]["email"], "admin_test_orc@myco.odoo.com",
        )

        self.assertEqual(len(push_calls), 1)
        # Gateway identity uses qualified email; Odoo API auth uses bare login.
        self.assertEqual(push_calls[0]["email"], "admin_test_orc@myco.odoo.com")
        self.assertEqual(push_calls[0]["odoo_login"], "admin_test_orc")


class TestOrcManagerGroupIsGranted(TransactionCase):
    """The addon must not install invisible.

    Everything this addon puts in the UI is gated on
    ``group_orc_manager``: the AI Workplace page on the user form, both
    menus, and read access to the audit log. Nothing else grants that group,
    so without the ``base.group_system`` implication in
    ``security/orc_security.xml`` the addon installs successfully and is then
    unreachable — including for the admin who installed it, who has no way to
    discover why. That is how it shipped on the 15.0 branch, and a green
    suite said nothing about it.

    On v15 a group the user lacks does not remove the node: ``_apply_groups``
    sets ``invisible="1"`` on it and drops the ``groups`` attribute
    (``ir_ui_view.py``). So the assertions below read the modifier, not the
    presence of the tag — checking for the tag passes either way and is the
    trap that let this through.
    """

    PAGE = 'name="orc_access"'

    def _page_is_visible(self, user):
        arch = self.env(user=user.id)["res.users"].fields_view_get(
            view_id=self.env.ref("base.view_users_form").id,
            view_type="form",
        )["arch"]
        start = arch.index("<page")
        while start != -1:
            end = arch.index(">", start) + 1
            tag = arch[start:end]
            if self.PAGE in tag:
                return 'invisible="1"' not in tag
            start = arch.find("<page", end)
        raise AssertionError("the AI Workplace page is not in the arch at all")

    def test_a_settings_admin_gets_the_manager_group(self):
        admin = self.env.ref("base.user_admin")
        self.assertTrue(admin.has_group("base.group_system"))
        self.assertTrue(
            admin.has_group("orc_client_provisioning.group_orc_manager"),
            "base.group_system must imply group_orc_manager, or the addon is "
            "invisible to every user on a fresh install",
        )

    def test_the_user_form_page_is_visible_to_an_admin(self):
        self.assertTrue(self._page_is_visible(self.env.ref("base.user_admin")))

    def test_the_user_form_page_stays_hidden_from_a_plain_user(self):
        # The other half: granting it to Settings must not grant it to
        # everybody. This is what makes the gate still worth having.
        plain = self.env["res.users"].create({
            "name": "Plain Internal",
            "login": "orc_plain_internal_for_group_test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        self.assertFalse(
            plain.has_group("orc_client_provisioning.group_orc_manager"))
        self.assertFalse(self._page_is_visible(plain))

    def test_the_menus_follow_the_same_group(self):
        admin = self.env.ref("base.user_admin")
        plain = self.env["res.users"].create({
            "name": "Plain Internal 2",
            "login": "orc_plain_internal_for_menu_test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        Menu = self.env["ir.ui.menu"]
        self.assertTrue(
            Menu.with_user(admin).search([("name", "ilike", "AI Workplace")]),
            "an admin must be able to reach the AI Workplace menus",
        )
        self.assertFalse(
            Menu.with_user(plain).search([("name", "ilike", "AI Workplace")]))
