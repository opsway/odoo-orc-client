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

    def _list_users_response(self, role):
        return {
            "users": [{"email": self.user.login, "role": role}],
            "infrastructures": [],
        }

    def test_role_drift_read_triggers_rotation(self):
        """ORC says user_readonly but local is write → rotate and flip level."""
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
            list_users=lambda *a, **kw: self._list_users_response("user_readonly"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_access_level, "read")
        self.assertEqual(self.user.orc_api_key_id.orc_access_level, "read")
        self.assertGreaterEqual(calls["push"], 1)
        self.assertEqual(calls["last_level"], "read")

    def test_role_drift_write_triggers_rotation(self):
        """Flip the other way: local=read, ORC=user → rotate to write."""
        self.user.sudo().write({"orc_access_level": "read"})
        self.user.orc_api_key_id.sudo().write({"orc_access_level": "read"})

        calls = {"last_level": None}

        def fake_push(**kw):
            calls["last_level"] = kw.get("access_level")

        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user"),
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_access_level, "write")
        self.assertEqual(calls["last_level"], "write")

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

    def test_matching_role_is_no_op(self):
        """ORC role matches local level → no rotation."""
        calls = {"push": 0}

        def fake_push(**kw):
            calls["push"] += 1

        # Local already write; ORC says 'user'.
        with patch.multiple(
            self.env["orc.client"],
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=fake_push,
            list_users=lambda *a, **kw: self._list_users_response("user"),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["push"], 0)
