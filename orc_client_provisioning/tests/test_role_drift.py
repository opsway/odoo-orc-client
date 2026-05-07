"""Two-way reconcile coverage for ``_cron_orc_reconcile``.

The cron uses Odoo as the source of truth for membership (who has
``orc_enabled=True``) and reaches into ORC to push or pull state to
match. Each per-user branch stamps ``orc_last_sync_*`` so admins can
see staleness on the user form. ORC's HTTP client is mocked here so
the tests can hand-craft payloads + force errors without hitting the
network.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase


class TestReconcile(TransactionCase):
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
        # Patch the orc.client class - patching the recordset only
        # sticks for one ``env`` lookup and the cron / write hook does
        # fresh lookups.
        self.client_cls = type(self.env["orc.client"])
        with patch.multiple(
            self.client_cls,
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=lambda *a, **kw: None,
        ):
            self.user.orc_enabled = True

    def _list_users_response(self, *emails_with_role):
        return {
            "users": [{"email": email, "role": role}
                      for email, role in emails_with_role],
            "infrastructures": [],
        }

    def test_remote_only_with_no_local_user_logs_orphan(self):
        """ORC has a user with no matching res.users — orphan, log
        as drift; can't auto-create local users from the remote list.
        """
        with patch.multiple(
            self.client_cls,
            list_users=lambda *a, **kw: self._list_users_response(
                (self.user.login, "member"),
                ("ghost@acme.test", "member"),
            ),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        log = self.env["orc.audit.log"].search([
            ("action", "=", "orphan_remote_user"),
            ("status", "=", "drift"),
        ], limit=1)
        self.assertTrue(log)
        self.assertIn("ghost@acme.test", log.error)

    def test_no_drift_stamps_ok(self):
        """Healthy in-sync user is stamped ok; no drift / error rows
        from the reconcile pass."""
        with patch.multiple(
            self.client_cls,
            list_users=lambda *a, **kw: self._list_users_response(
                (self.user.login, "member"),
            ),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        log = self.env["orc.audit.log"].search([
            ("action", "in", ["reconcile", "orphan_remote_user"]),
            ("status", "in", ["drift", "error"]),
        ])
        self.assertFalse(log)
        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertTrue(self.user.orc_last_sync_at)

    def test_local_enabled_remote_missing_reprovisions(self):
        """Direction A: local says enabled, remote doesn't have the
        user. Cron must call provision_user and stamp ok."""
        calls = {"provision": 0}

        def fake_provision(*a, **kw):
            calls["provision"] += 1
            return "orc-uid-1"

        with patch.multiple(
            self.client_cls,
            provision_user=fake_provision,
            push_odoo_key=lambda *a, **kw: None,
            list_users=lambda *a, **kw: {"users": [], "infrastructures": []},
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["provision"], 1)
        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("re-provisioned", self.user.orc_last_sync_message or "")

    def test_residual_remote_with_disabled_local_revokes(self):
        """Direction B: remote has the user, local says
        ``orc_enabled=False``. Cron must call ``revoke_infra_access``
        and stamp ok on the local row."""
        # Flip local to disabled. The on-save hook calls
        # ``revoke_infra_access`` which we patch to a no-op so the
        # local row ends up with ``orc_enabled=False`` while ORC
        # still considers the user enrolled - exactly the residual
        # state Direction B reconciles.
        self.user.invalidate_cache()
        with patch.multiple(
            self.client_cls,
            revoke_infra_access=lambda *a, **kw: None,
        ):
            self.user.orc_enabled = False

        calls = {"revoke": 0}

        def fake_revoke(*a, **kw):
            calls["revoke"] += 1

        with patch.multiple(
            self.client_cls,
            revoke_infra_access=fake_revoke,
            list_users=lambda *a, **kw: self._list_users_response(
                (self.user.login, "member"),
            ),
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertEqual(calls["revoke"], 1)
        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("deprovisioned from ORC", self.user.orc_last_sync_message or "")

    def test_list_users_failure_stamps_all_enabled_users_error(self):
        """A failure in ``list_users`` itself stamps every
        ``orc_enabled=True`` user as error so an outage surfaces on
        every dashboard immediately."""
        def boom(*a, **kw):
            raise UserError("ORC unreachable")

        with patch.multiple(
            self.client_cls,
            list_users=boom,
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.user.invalidate_cache()
        self.assertEqual(self.user.orc_last_sync_status, "error")
        self.assertIn("ORC unreachable", self.user.orc_last_sync_message or "")

    def test_member_role_only(self):
        """Provisioning never sends ``admin`` from this addon; admin
        promotion is an ORC-dashboard action, not Odoo's."""
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({"groups_id": [(4, manager_group.id)]})
        self.user.invalidate_cache()
        self.assertTrue(self.user.orc_is_manager)

        calls = {"role": None}

        def fake_provision(*a, **kw):
            calls["role"] = kw.get("role")
            return "orc-uid-1"

        with patch.multiple(
            self.client_cls,
            provision_user=fake_provision,
            push_odoo_key=lambda *a, **kw: None,
        ):
            self.user.action_orc_provision()
        self.assertEqual(calls["role"], "member")
