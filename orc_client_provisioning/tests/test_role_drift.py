"""_cron_orc_reconcile's drift detection.

Mocks ORC's list_users so the test can hand-craft payloads.

Per INT-842: per-user RPC access (read/write) was dropped, and the
addon no longer manages the admin tier (admin promotion is a
dashboard action). What remains is membership drift logging only —
no auto re-provision.
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

    def test_remote_only_logged_as_drift(self):
        """ORC has a user we don't — log as drift, no action."""
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
            ("action", "=", "reconcile"),
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
        log = self.env["orc.audit.log"].search([
            ("action", "=", "reconcile"),
            ("status", "=", "drift"),
        ])
        self.assertFalse(log)

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
