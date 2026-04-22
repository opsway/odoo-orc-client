from unittest.mock import patch

from odoo.exceptions import UserError
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
            "deprovision_user": lambda **kw: None,
        }
        defaults.update(overrides)
        return patch.multiple(client, **{k: lambda *a, **kw: v for k, v in defaults.items()
                                          if not callable(v)},
                              provision_user=defaults["provision_user"],
                              push_odoo_key=defaults["push_odoo_key"],
                              deprovision_user=defaults["deprovision_user"])

    def test_provision_creates_key_and_records_audit(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_user_id, "orc-uid-1")
        self.assertTrue(self.user.orc_api_key_id)
        self.assertEqual(self.user.orc_api_key_id.name, "ORC (auto-managed)")
        self.assertTrue(self.user.orc_provisioned_at)
        self.assertTrue(self.user.orc_last_rotation_at)
        # Default orc_access_level is 'read'; it must propagate to the key row.
        self.assertEqual(self.user.orc_access_level, "read")
        self.assertEqual(self.user.orc_api_key_id.orc_access_level, "read")
        log = self.env["orc.audit.log"].search([("user_id", "=", self.user.id)], limit=1)
        self.assertEqual(log.action, "provision")
        self.assertEqual(log.status, "ok")

    def test_provision_propagates_write_level_to_key(self):
        self.user.orc_access_level = "write"
        captured = {}

        def fake_push(**kw):
            captured.update(kw)

        with self._patch_client(push_odoo_key=fake_push):
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_api_key_id.orc_access_level, "write")
        # push_odoo_key receives the level so ORC can mirror it.
        self.assertEqual(captured.get("access_level"), "write")

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

    def test_deprovision_revokes_and_clears(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.assertTrue(self.user.orc_user_id)
        key_id = self.user.orc_api_key_id.id

        with self._patch_client():
            self.user.orc_enabled = False

        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_enabled)
        self.assertFalse(self.user.orc_user_id)
        self.assertFalse(self.user.orc_api_key_id)
        # Key row is gone.
        self.assertFalse(self.env["res.users.apikeys"].search([("id", "=", key_id)]))
