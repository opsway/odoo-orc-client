"""Cron coverage for the provisioning addon.

The hourly `_cron_orc_sync` → `_cron_orc_reconcile` does two-way
membership sync (provision missing remote, revoke disabled remote);
the daily `_cron_orc_maintenance` → `_cron_orc_rotate_keys` rotates
keys past their TTL. Both stamp `orc_last_sync_*` per user so the
form view reflects the cron's last verdict.

Tests mock the ORC HTTP client so they can hand-craft remote
payloads + force errors without hitting the network.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase


class TestReconcileDrift(TransactionCase):
    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("orc.endpoint_url", "https://orc.test")
        icp.set_param("orc.org_token", "orc_test_token")
        icp.set_param("orc.infrastructure_id", "11111111-1111-1111-1111-111111111111")

        self.user = self.env["res.users"].create({
            "name": "Alice Example",
            "login": "alice@acme.test",
        })
        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=lambda *a, **kw: None,
        ):
            self.user.orc_enabled = True

    def test_remote_only_with_no_local_user_logs_orphan(self):
        """ORC has a user with no matching res.users — orphan, log
        as drift; can't auto-create local users from the remote list."""
        with patch.multiple(
            self.env["orc.client"],
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user"},
                    {"email": "ghost@acme.test", "role": "user"},
                ],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()
        log = self.env["orc.audit.log"].search([
            ("action", "=", "orphan_remote_user"),
            ("status", "=", "drift"),
        ], limit=1)
        self.assertTrue(log)
        self.assertIn("ghost@acme.test", log.error)

    def test_no_drift_no_log(self):
        with patch.multiple(
            self.env["orc.client"],
            list_users=lambda *a, **kw: {
                "users": [{"email": self.user.login, "role": "user"}],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()
        # No drift / orphan / error rows from this pass.
        log = self.env["orc.audit.log"].search([
            ("action", "in", ["reconcile", "orphan_remote_user"]),
            ("status", "in", ["drift", "error"]),
        ])
        self.assertFalse(log)
        # Healthy in-sync user is stamped ok.
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertTrue(self.user.orc_last_sync_at)

    def test_reconcile_local_enabled_remote_missing_reprovisions(self):
        """Direction A — local says enabled, remote doesn't have
        the user. Cron must call provision_user and stamp ok."""
        calls = {"provision": 0}

        def fake_provision(**kw):
            calls["provision"] += 1
            return "orc-uid-1"

        with patch.multiple(
            self.env["orc.client"],
            list_users=lambda *a, **kw: {"users": [], "infrastructures": []},
            provision_user=fake_provision,
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertEqual(calls["provision"], 1, "expected one re-provision call")
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertTrue(self.user.orc_last_sync_at)
        self.assertIn("re-provisioned", self.user.orc_last_sync_message or "")

    def test_reconcile_remote_present_local_disabled_deprovisions(self):
        """Direction B — local user exists with orc_enabled=False
        but remote still lists them. Cron must call revoke and stamp ok."""
        # Flip the user off — but pretend remote still has them (drift).
        with patch.multiple(
            self.env["orc.client"],
            revoke_infra_access=lambda **kw: None,
        ):
            self.user.orc_enabled = False

        calls = {"revoke_email": None}

        def fake_revoke(**kw):
            calls["revoke_email"] = kw.get("email")

        with patch.multiple(
            self.env["orc.client"],
            list_users=lambda *a, **kw: {
                "users": [{"email": self.user.login, "role": "user"}],
                "infrastructures": [],
            },
            revoke_infra_access=fake_revoke,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertEqual(calls["revoke_email"], self.user.login)
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("deprovisioned", self.user.orc_last_sync_message or "")

    def test_reconcile_http_error_marks_user_error(self):
        """Network/auth blip on list_users → every orc_enabled user
        gets a red badge with the error message."""
        from odoo.exceptions import UserError as Boom

        def fail(*a, **kw):
            raise Boom("upstream 500")

        with patch.multiple(self.env["orc.client"], list_users=fail):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "error")
        self.assertIn("upstream 500", self.user.orc_last_sync_message or "")
        self.assertTrue(self.user.orc_last_sync_at)
        # And the failure shows up as a single audit-log entry.
        log = self.env["orc.audit.log"].search([
            ("action", "=", "reconcile"),
            ("status", "=", "error"),
        ], limit=1)
        self.assertTrue(log)
        self.assertIn("upstream 500", log.error)

    def test_addon_provisions_as_user_regardless_of_manager_group(self):
        """Manager group no longer auto-promotes to ORC admin —
        addon always sends role='user'."""
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({"groups_id": [(4, manager_group.id)]})
        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_is_manager)

        calls = {"role": None}

        def fake_provision(**kw):
            calls["role"] = kw.get("role")
            return "orc-uid-1"

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=lambda **kw: None,
        ):
            self.user.action_orc_provision()

        self.assertEqual(calls["role"], "user")

    # ---- _cron_orc_rotate_keys -------------------------------------------
    # The daily maintenance cron rotates keys past their TTL via
    # `action_orc_provision()`. Per-user failures must NOT abort the
    # batch and must stamp the user as "error" so the form badge shows
    # the failure (the previous swallow-and-continue path was
    # invisible to admins).

    def _force_rotation_due(self, user):
        """Set orc.rotation_days=0 so every enrolled user is past TTL."""
        self.env["ir.config_parameter"].sudo().set_param("orc.rotation_days", "0")
        # action_orc_provision() in setUp left orc_last_rotation_at = now;
        # with rotation_days=0 the cron's "< cutoff" predicate matches.

    def test_rotate_stamps_ok_on_success(self):
        self._force_rotation_due(self.user)
        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_rotate_keys()
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("rotated", self.user.orc_last_sync_message or "")
        self.assertTrue(self.user.orc_last_sync_at)

    def test_rotate_stamps_error_and_writes_audit_log(self):
        from odoo.exceptions import UserError
        self._force_rotation_due(self.user)

        def fail(**kw):
            raise UserError("ORC down")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fail,
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_rotate_keys()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "error")
        self.assertIn("ORC down", self.user.orc_last_sync_message or "")
        log = self.env["orc.audit.log"].search([
            ("user_id", "=", self.user.id),
            ("action", "=", "rotate"),
            ("status", "=", "error"),
        ], limit=1)
        self.assertTrue(log)

    # ---- Wrapper crons ---------------------------------------------------
    # `_cron_orc_sync` and `_cron_orc_maintenance` are thin wrappers
    # that compose the work above; smoke-test them once each so a
    # rename in res_users.py can't silently break the ir.cron XML.

    def test_cron_orc_sync_runs_reconcile(self):
        with patch.multiple(
            self.env["orc.client"],
            list_users=lambda *a, **kw: {
                "users": [{"email": self.user.login, "role": "user"}],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_sync()
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")

    def test_cron_orc_maintenance_runs_rotate(self):
        self._force_rotation_due(self.user)
        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_maintenance()
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("rotated", self.user.orc_last_sync_message or "")
