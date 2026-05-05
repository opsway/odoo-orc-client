"""_cron_orc_reconcile's role-drift detection + rotation.

Mocks ORC's list_users and asserts the local addon reasserts authority
(Odoo group decides admin/user; ORC decides read/write within user).
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
        # Default orc_access_level is 'read'; for drift tests start at write.
        self.user.sudo().write({"orc_access_level": "write"})

    def _list_users_response(self, role, odoo_access=None):
        u = {"email": self.user.login, "role": role}
        if odoo_access is not None:
            u["odoo_access"] = odoo_access
        return {"users": [u], "infrastructures": []}

    def test_capability_drift_read_triggers_rotation(self):
        calls = {"push": 0, "last_level": None}

        def fake_push(*a, **kw):
            calls["push"] += 1
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user", "read"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_access_level, "read")
        self.assertGreaterEqual(calls["push"], 1)
        self.assertEqual(calls["last_level"], "read")

    def test_capability_drift_write_triggers_rotation(self):
        self.user.sudo().write({"orc_access_level": "read"})

        calls = {"last_level": None}

        def fake_push(*a, **kw):
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user", "write"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_access_level, "write")
        self.assertEqual(calls["last_level"], "write")

    def test_legacy_user_readonly_role_maps_to_read(self):
        calls = {"last_level": None}

        def fake_push(*a, **kw):
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user_readonly"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_access_level, "read")
        self.assertEqual(calls["last_level"], "read")

    def test_no_role_no_drift(self):
        calls = {"push": 0}

        def fake_push(*a, **kw):
            calls["push"] += 1

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: {
                "users": [{"email": self.user.login}],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["push"], 0)

    def test_matching_capability_is_no_op(self):
        calls = {"push": 0}

        def fake_push(*a, **kw):
            calls["push"] += 1

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user", "write"),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["push"], 0)

    def test_tier_drift_user_to_admin_reprovisions(self):
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({"groups_id": [(4, manager_group.id)]})
        self.user.invalidate_cache()
        self.assertTrue(self.user.orc_is_manager)

        calls = {"last_role": None}

        def fake_provision(*a, **kw):
            calls["last_role"] = kw.get("role")
            return "orc-uid-1"

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=lambda *a, **kw: None,
            list_users=lambda *a, **kw: self._list_users_response("user"),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["last_role"], "admin")

    def test_tier_drift_admin_to_user_reprovisions(self):
        calls = {"last_role": None}

        def fake_provision(*a, **kw):
            calls["last_role"] = kw.get("role")
            return "orc-uid-1"

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=lambda *a, **kw: None,
            list_users=lambda *a, **kw: self._list_users_response("admin"),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        # Local is access_level=write -> non-manager, write -> "user".
        self.assertEqual(calls["last_role"], "user")

    def test_admin_tier_ignores_read_write_knob(self):
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({
            "groups_id": [(4, manager_group.id)],
            "orc_access_level": "read",
        })
        self.user.invalidate_cache()

        calls = {"role": None, "level": None}

        def fake_provision(*a, **kw):
            calls["role"] = kw.get("role")
            return "orc-uid-1"

        def fake_push(*a, **kw):
            calls["level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=fake_provision,
            push_odoo_key=fake_push,
        ):
            self.user.action_orc_provision()
        self.assertEqual(calls["role"], "admin")
        self.assertEqual(calls["level"], "write")
