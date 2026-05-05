from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase


class TestOrcProvisioning(TransactionCase):
    """Lifecycle tests against the new field layout (key on res.users).

    All ORC HTTP calls are mocked - the tests verify the local-side
    branches: key generation, key clearing, audit log entries, write/
    deprovision interlocks.
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
            "provision_user": lambda *a, **kw: "orc-uid-1",
            "push_odoo_key": lambda *a, **kw: None,
            "deprovision_user": lambda *a, **kw: None,
        }
        defaults.update(overrides)
        return patch.multiple(client, **defaults)

    def test_provision_creates_key_and_records_audit(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_cache()
        s = self.user.sudo()
        self.assertEqual(s.orc_user_id, "orc-uid-1")
        self.assertTrue(s.orc_api_key_hash)
        self.assertTrue(s.orc_api_key_index)
        self.assertEqual(len(s.orc_api_key_index), 8)
        self.assertTrue(s.orc_api_key_rotated_at)
        self.assertTrue(s.orc_api_key_expires_at)
        self.assertTrue(s.orc_provisioned_at)
        self.assertEqual(s.orc_access_level, "read")
        log = self.env["orc.audit.log"].search(
            [("user_id", "=", self.user.id), ("action", "=", "provision")],
            limit=1,
        )
        self.assertTrue(log)
        self.assertEqual(log.status, "ok")

    def test_provision_propagates_write_level(self):
        self.user.orc_access_level = "write"
        captured = {}

        def fake_push(*a, **kw):
            captured.update(kw)

        with self._patch_client(push_odoo_key=fake_push):
            self.user.orc_enabled = True
        self.assertEqual(captured.get("access_level"), "write")

    def test_provision_rollback_on_push_failure(self):
        def fail_push(*a, **kw):
            raise UserError("boom")

        with self._patch_client(push_odoo_key=fail_push):
            with self.assertRaises(UserError):
                self.user.orc_enabled = True

        # Transaction rolls back -> no orc_user_id, no key fields.
        self.user.invalidate_cache()
        s = self.user.sudo()
        self.assertFalse(s.orc_user_id)
        self.assertFalse(s.orc_api_key_hash)
        self.assertFalse(s.orc_api_key_index)

    def test_deprovision_clears_key_and_logs(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.assertTrue(self.user.sudo().orc_user_id)

        with self._patch_client():
            self.user.orc_enabled = False

        self.user.invalidate_cache()
        s = self.user.sudo()
        self.assertFalse(s.orc_enabled)
        self.assertFalse(s.orc_user_id)
        self.assertFalse(s.orc_api_key_hash)
        self.assertFalse(s.orc_api_key_index)
        log = self.env["orc.audit.log"].search(
            [("user_id", "=", self.user.id), ("action", "=", "deprovision")],
            limit=1,
        )
        self.assertEqual(log.status, "ok")
