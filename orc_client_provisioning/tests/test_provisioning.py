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
            "revoke_infra_access": lambda **kw: None,
        }
        defaults.update(overrides)
        return patch.multiple(client, **{k: lambda *a, **kw: v for k, v in defaults.items()
                                          if not callable(v)},
                              provision_user=defaults["provision_user"],
                              push_odoo_key=defaults["push_odoo_key"],
                              revoke_infra_access=defaults["revoke_infra_access"])

    def test_provision_creates_key_and_records_audit(self):
        with self._patch_client():
            self.user.orc_enabled = True
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_user_id, "orc-uid-1")
        self.assertTrue(self.user.orc_api_key_id)
        self.assertEqual(self.user.orc_api_key_id.name, "ORC (auto-managed)")
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
        # ORC was told to revoke BY EMAIL (not by orc_user_id) — the
        # per-infra endpoint is acting-user-scoped.
        self.assertEqual(revoke_calls, [{"email": self.user.login}])

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
