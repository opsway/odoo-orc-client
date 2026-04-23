"""_cron_orc_reconcile's role-drift detection + rotation.

Mocks ORC's list_users so the test can hand-craft role payloads and
verify the local addon reacts correctly. No network, no real ORC.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase


class TestRoleDrift(TransactionCase):
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
        # Default orc_access_level is 'read'; for drift tests we flip to
        # 'write' and make ORC say 'user_readonly' below.
        self.user.sudo().write({"orc_access_level": "write"})
        self.user.orc_api_key_id.sudo().write({"orc_access_level": "write"})

    def _list_users_response(self, role, odoo_access=None):
        u = {"email": self.user.login, "role": role}
        if odoo_access is not None:
            u["odoo_access"] = odoo_access
        return {"users": [u], "infrastructures": []}

    def test_capability_drift_read_triggers_rotation(self):
        """ORC says odoo_access=read but local is write → rotate and flip level."""
        calls = {"provision": 0, "push": 0, "last_level": None}

        def fake_provision(**kw):
            calls["provision"] += 1
            return "orc-uid-1"

        def fake_push(**kw):
            calls["push"] += 1
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user", "read"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_access_level, "read")
        self.assertEqual(self.user.orc_api_key_id.orc_access_level, "read")
        self.assertGreaterEqual(calls["push"], 1)
        self.assertEqual(calls["last_level"], "read")

    def test_capability_drift_write_triggers_rotation(self):
        """Flip the other way: local=read, ORC=write → rotate to write."""
        self.user.sudo().write({"orc_access_level": "read"})
        self.user.orc_api_key_id.sudo().write({"orc_access_level": "read"})

        calls = {"last_level": None}

        def fake_push(**kw):
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user", "write"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_access_level, "write")
        self.assertEqual(calls["last_level"], "write")

    def test_legacy_user_readonly_role_maps_to_read(self):
        """Legacy ORC returns role='user_readonly' → treat as user + access=read."""
        calls = {"last_level": None}

        def fake_push(**kw):
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user_readonly"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_access_level, "read")
        self.assertEqual(calls["last_level"], "read")

    def test_no_role_no_drift(self):
        """ORC omits role in response → reconcile treats as no-op (no rotation)."""
        calls = {"push": 0}

        def fake_push(**kw):
            calls["push"] += 1

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: {
                "users": [{"email": self.user.login}],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_access_level, "write")
        self.assertEqual(calls["push"], 0)

    def test_matching_capability_is_no_op(self):
        """ORC capability matches local level → no rotation."""
        calls = {"push": 0}

        def fake_push(**kw):
            calls["push"] += 1

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user", "write"),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["push"], 0)

    # --- Tier drift (admin vs user) ----------------------------------------

    def test_tier_drift_user_to_admin_reprovisions(self):
        """Odoo user joined manager group but ORC still says 'user' → re-provision."""
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({"groups_id": [(4, manager_group.id)]})
        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_is_manager)

        calls = {"provision": 0, "last_role": None}

        def fake_provision(**kw):
            calls["provision"] += 1
            calls["last_role"] = kw.get("role")
            return "orc-uid-1"

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=lambda **kw: None,
            list_users=lambda *a, **kw: self._list_users_response("user"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertGreaterEqual(calls["provision"], 1)
        self.assertEqual(calls["last_role"], "admin")

    def test_tier_drift_admin_to_user_reprovisions(self):
        """ORC says 'admin' but Odoo user is not a manager → demote via re-provision."""
        # user already not in the manager group (default)
        calls = {"provision": 0, "last_role": None}

        def fake_provision(**kw):
            calls["provision"] += 1
            calls["last_role"] = kw.get("role")
            return "orc-uid-1"

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=lambda **kw: None,
            list_users=lambda *a, **kw: self._list_users_response("admin"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertGreaterEqual(calls["provision"], 1)
        # Local is access_level=write → non-manager, write → "user".
        self.assertEqual(calls["last_role"], "user")

    def test_admin_tier_ignores_read_write_knob(self):
        """Manager with local access_level='read' still provisions as admin (write)."""
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({
            "groups_id": [(4, manager_group.id)],
            "orc_access_level": "read",
        })
        self.user.invalidate_recordset()

        calls = {"role": None, "level": None}

        def fake_provision(**kw):
            calls["role"] = kw.get("role")
            return "orc-uid-1"

        def fake_push(**kw):
            calls["level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=fake_push,
        ):
            self.user.action_orc_provision()

        self.assertEqual(calls["role"], "admin")
        self.assertEqual(calls["level"], "write")
