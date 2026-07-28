from unittest.mock import patch

from odoo.exceptions import MissingError, UserError
from odoo.tests import TransactionCase


class TestOrcProvisioning(TransactionCase):
    """
    Exercises the Odoo-side branches of action_orc_provision /
    action_orc_deprovision. All ORC HTTP calls are mocked — the tests
    verify the lifecycle (key create / key revoke / row updates /
    audit log) without hitting the network.
    """

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Alice Example",
            "login": "alice@acme.test",
        })
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("orc.endpoint_url", "https://orc.test")
        icp.set_param("orc.org_token", "orc_test_token")
        icp.set_param("orc.infrastructure_id", "11111111-1111-1111-1111-111111111111")

    def _patch_client(self, **overrides):
        client = self.env["orc.client"]
        defaults = {
            "provision_user": lambda **kw: "orc-uid-1",
            "push_odoo_key": lambda **kw: None,
            "revoke_infra_access": lambda **kw: None,
            # Default: the gateway knows about nobody. Reconcile tests override
            # this to place a user in the remote directory.
            "list_users": lambda **kw: {"users": []},
        }
        defaults.update(overrides)
        return patch.multiple(
            client,
            provision_user=defaults["provision_user"],
            push_odoo_key=defaults["push_odoo_key"],
            revoke_infra_access=defaults["revoke_infra_access"],
            list_users=defaults["list_users"],
        )

    def test_provision_creates_key_and_records_audit(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_user_id, "orc-uid-1")
        self.assertTrue(self.user.orc_api_key_id)
        self.assertEqual(self.user.orc_api_key_id.name, "AI Workplace (auto-managed)")
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
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertTrue(self.user.orc_last_sync_at)
        self.assertIn("provisioned", self.user.orc_last_sync_message or "")

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
        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_user_id)
        self.assertFalse(self.user.orc_api_key_id)

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
        key_id = self.user.orc_api_key_id.id

        with self._patch_client(revoke_infra_access=capture_revoke):
            self.user.orc_enabled = False

        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_enabled)
        # Breadcrumb retained.
        self.assertEqual(self.user.orc_user_id, orc_uid)
        # Managed key row on Odoo side is gone.
        self.assertFalse(self.user.orc_api_key_id)
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

        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_api_key_id)  # fresh key pushed
        # provision_user was actually called despite the breadcrumb
        # being present (write-hook keys off `orc_api_key_id`, not
        # `orc_user_id`, to catch re-enrolment).
        self.assertEqual(len(provision_calls), 1)

    def test_orphan_cleanup_clears_dangling_apikey_pointer(self):
        """A managed key hard-deleted out-of-band leaves orc_api_key_id
        dangling, which breaks every read of the user form.

        Odoo core GCs expired api keys with a raw-SQL DELETE
        (`_gc_user_apikeys`). Because `res.users.apikeys` is `_auto=False`
        there is no real DB FK, so the field's `ondelete="set null"`
        (enforced only by the ORM unlink) never fires and the pointer is
        left referencing a row that no longer exists. Reading the linked
        key — as the user form does to render the Many2one — then raises
        MissingError. The nightly orphan-cleanup cron must heal it.
        """
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
        key_id = self.user.orc_api_key_id.id
        self.assertTrue(key_id)

        # Simulate core's raw-SQL GC of the expired key: bypasses the
        # ORM unlink, so ondelete="set null" never runs.
        self.env.cr.execute(
            "DELETE FROM res_users_apikeys WHERE id = %s", (key_id,)
        )
        self.env.invalidate_all()

        # Repro: the dangling pointer makes the user form unrenderable.
        with self.assertRaises(MissingError):
            self.user.orc_api_key_id.display_name

        # Nightly cleanup clears the dangling pointer.
        self.env["res.users"]._cron_orc_orphan_cleanup()

        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_api_key_id)
        # The user form now reads end-to-end (no MissingError).
        self.user.web_read({"orc_api_key_id": {"fields": {"display_name": {}}}})

    # -- rotation key-pointer / reconcile-validity regression -------------------

    def test_reconcile_reprovisions_when_local_key_pointer_lost(self):
        """Regression for the rotation data-loss bug.

        A rotation once left `orc_api_key_id` empty (the outer-transaction
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
        self.user.invalidate_recordset()
        email = self.user._orc_gateway_identity()

        # Simulate the drift: gateway still lists the user, local pointer gone.
        self.user.sudo().write({"orc_api_key_id": False})

        provision_calls: list[dict] = []

        def capture_provision(**kw):
            provision_calls.append(kw)
            return self.user.orc_user_id or "orc-uid-1"

        with self._patch_client(
            provision_user=capture_provision,
            list_users=lambda **kw: {"users": [{"email": email}]},
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(len(provision_calls), 1,
                         "lost pointer must trigger re-provision, not 'in sync'")
        self.assertTrue(self.user.orc_api_key_id, "ownership pointer restored")
        self.assertIn("healed", (self.user.orc_last_sync_message or "").lower())

    def test_reconcile_stays_in_sync_when_pointer_is_valid(self):
        """The validity guard must NOT re-provision a healthy user — a present,
        existing local key + a remote key row is genuinely 'in sync'."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
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

        self.user.invalidate_recordset()
        self.assertEqual(provision_calls, [],
                         "valid pointer → in sync, must not re-provision")
        self.assertEqual(self.user.orc_last_sync_message, "in sync")

    def test_orphan_reaper_respects_grace_window(self):
        """A freshly-created managed key must survive the orphan reaper even if
        momentarily unreferenced — that race is what silently deleted rotated
        keys. Only keys older than the grace window are genuine orphans."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
        key_id = self.user.orc_api_key_id.id
        self.assertTrue(key_id)

        # Make it unreferenced (the lost-pointer state).
        self.user.sudo().write({"orc_api_key_id": False})

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
        self.env.invalidate_all()
        self.env["res.users"]._cron_orc_orphan_cleanup()
        self.assertFalse(
            self.env["res.users.apikeys"].browse(key_id).exists(),
            "an aged unreferenced managed key must be reaped",
        )

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
        admin.invalidate_recordset()
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
        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_enabled)
        prior_uid = self.user.orc_user_id
        self.assertTrue(prior_uid)

        # Write that changes login.  No need to mock the client — the
        # write override must short-circuit before any provisioning.
        with self._patch_client():
            self.user.sudo().write({"login": "renamed@acme.test"})

        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_enabled)
        # Breadcrumb retained — re-enabling re-provisions cleanly
        # against the new login.
        self.assertEqual(self.user.orc_user_id, prior_uid)

    def test_write_no_login_change_preserves_orc_enabled(self):
        """The login-change guard only fires on an actual change.
        Writing the same login back is a no-op for orc_enabled."""
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()

        # Same value — guard must NOT trip.
        with self._patch_client():
            self.user.sudo().write({"login": self.user.login})

        self.user.invalidate_recordset()
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

        self.user.invalidate_recordset()
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
