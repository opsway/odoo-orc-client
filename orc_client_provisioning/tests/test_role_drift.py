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

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase

from .common import patch_orc_client, share_test_cursor


class TestReconcileDrift(TransactionCase):
    def setUp(self):
        super().setUp()
        share_test_cursor(self)
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("orc.endpoint_url", "https://orc.test")
        icp.set_param("orc.org_token", "orc_test_token")
        icp.set_param("orc.infrastructure_id", "11111111-1111-1111-1111-111111111111")

        self.user = self.env["res.users"].create({
            "name": "Alice Example",
            "login": "alice@acme.test",
        })
        with patch_orc_client(
            self.env,
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=lambda *a, **kw: None,
        ):
            self.user.orc_enabled = True

    def test_remote_only_with_no_local_user_logs_orphan(self):
        """ORC has a user with no matching res.users — orphan, log
        as drift; can't auto-create local users from the remote list."""
        with patch_orc_client(
            self.env,
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

    def test_residual_failure_does_not_abort_the_whole_reconcile(self):
        """Direction B is isolated per remote entry.

        The revoke call was already wrapped; the orphan audit-row write was
        NOT, and that is the one that fired in production (an action value
        missing from the Selection). Anything raised there escaped
        `_cron_orc_reconcile` and rolled the tick back — remaining residual
        entries unprocessed, every Direction-A stamp discarded, and the cron
        looking like it had never run.

        So force the failure where the gap actually was: make the orphan write
        raise. Two orphans, and we assert BOTH were attempted — deliberately
        order-independent, since `residual_remote` is a set. Without per-entry
        isolation the loop stops after the first, whichever that is.
        """
        audit_cls = type(self.env["orc.audit.log"])
        real_create = audit_cls.create
        orphan_attempts = []

        def refuse_orphan_rows(model_self, vals_list):
            vals = vals_list[0] if isinstance(vals_list, list) and vals_list else vals_list
            if isinstance(vals, dict) and vals.get("action") == "orphan_remote_user":
                orphan_attempts.append(vals.get("error"))
                raise UserError("audit write refused")
            return real_create(model_self, vals_list)

        # alice stays enabled and present remotely, so Direction A stamps her
        # "in sync" BEFORE Direction B runs — that stamp is what a rollback
        # would silently destroy.
        with patch.object(audit_cls, "create", refuse_orphan_rows), patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user"},
                    {"email": "ghost-one@acme.test", "role": "user"},
                    {"email": "ghost-two@acme.test", "role": "user"},
                ],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertEqual(
            len(orphan_attempts), 2,
            "both orphans must be attempted — the loop stopped at the first",
        )
        # Direction A's work survived rather than being rolled back.
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertEqual(self.user.orc_last_sync_message, "in sync")

    def test_residual_revoke_failure_is_recorded_against_the_user(self):
        """A failing revoke stamps that user red and keeps going.

        Complements the test above: this covers the branch that WAS already
        guarded, so the two together pin both halves of the loop's contract.
        """
        with patch_orc_client(self.env, revoke_infra_access=lambda **kw: None):
            other = self.env["res.users"].create({
                "name": "Bob Example", "login": "bob@acme.test",
            })
            with patch_orc_client(
                self.env,
                provision_user=lambda *a, **kw: "orc-uid-2",
                push_odoo_key=lambda *a, **kw: None,
            ):
                other.orc_enabled = True
            other.orc_enabled = False

        def revoke_fails(**kw):
            raise UserError("boom on bob")

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user"},
                    {"email": other.login, "role": "user"},
                ],
                "infrastructures": [],
            },
            revoke_infra_access=revoke_fails,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        other.invalidate_recordset()
        self.assertEqual(other.orc_last_sync_status, "error")
        self.assertIn("boom on bob", other.orc_last_sync_message or "")
        self.assertTrue(self.env["orc.audit.log"].search([
            ("user_id", "=", other.id),
            ("action", "=", "reconcile"),
            ("status", "=", "error"),
        ], limit=1))

    def test_no_drift_no_log(self):
        with patch_orc_client(
            self.env,
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

        with patch_orc_client(
            self.env,
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
        with patch_orc_client(
            self.env,
            revoke_infra_access=lambda **kw: None,
        ):
            self.user.orc_enabled = False

        calls = {"revoke_email": None}

        def fake_revoke(**kw):
            calls["revoke_email"] = kw.get("email")

        with patch_orc_client(
            self.env,
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

    def test_reconcile_already_revoked_disabled_user_does_not_rerevoke(self):
        """Per-infra contract: a locally-disabled user whose key on
        this infra was already revoked must NOT appear in the remote
        response, so reconcile does not call revoke again on every
        cron tick (the org-scoped pre-1.9.0 endpoint kept returning
        them by org membership and produced revoke churn)."""
        with patch_orc_client(
            self.env,
            revoke_infra_access=lambda **kw: None,
        ):
            self.user.orc_enabled = False

        calls = {"revoke_count": 0}

        def fake_revoke(**kw):
            calls["revoke_count"] += 1

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {"users": []},
            revoke_infra_access=fake_revoke,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertEqual(calls["revoke_count"], 0)

    def test_reconcile_org_member_without_infra_key_reprovisions(self):
        """Per-infra contract: a user who's still in the org but
        lost their key on this infra must NOT appear in list_users()
        (the endpoint filters by per-infra key ownership), so the
        Direction-A branch re-provisions instead of stamping in sync.
        Pre-1.9.0 the org-scoped endpoint returned them and reconcile
        wrongly marked them ok, leaving the user unable to access this
        Odoo until an admin toggled them manually."""
        calls = {"provision": 0}

        def fake_provision(**kw):
            calls["provision"] += 1
            return "orc-uid-1"

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {"users": []},
            provision_user=fake_provision,
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertEqual(calls["provision"], 1)
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("re-provisioned", self.user.orc_last_sync_message or "")

    def test_reconcile_http_error_marks_user_error(self):
        """Network/auth blip on list_users → every orc_enabled user
        gets a red badge with the error message."""
        def fail(*a, **kw):
            raise UserError("upstream 500")

        with patch_orc_client(self.env, list_users=fail):
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

    def test_addon_never_sends_role_parameter_to_provision_user(self):
        """Plan §9.1 + task 63 — the addon dropped the `role` parameter.
        Admin / non-admin promotion is a platform_user concern handled
        by the AI Workplace dashboard's invite flow; the addon always
        provisions org_users (members) on the server side.  Manager
        group still drives view affordances (orc_is_manager) but does
        NOT escalate the gateway role."""
        manager_group = self.env.ref("orc_client_provisioning.group_orc_manager")
        self.user.sudo().write({"group_ids": [(4, manager_group.id)]})
        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_is_manager)

        calls = {"kwargs": None}

        def fake_provision(**kw):
            calls["kwargs"] = kw
            return "orc-uid-1"

        with patch_orc_client(
            self.env,
            provision_user=fake_provision,
            push_odoo_key=lambda **kw: None,
        ):
            self.user.action_orc_provision()

        # Positive: odoo_login + name are passed; email is the
        # qualified form for display.
        self.assertIn("odoo_login", calls["kwargs"])
        self.assertIn("name", calls["kwargs"])
        # Negative: `role` is gone from the signature.
        self.assertNotIn("role", calls["kwargs"])

    # ---- _cron_orc_rotate_keys -------------------------------------------
    # The daily maintenance cron rotates keys past their TTL via
    # `action_orc_provision()`. Per-user failures must NOT abort the
    # batch and must stamp the user as "error" so the form badge shows
    # the failure (the previous swallow-and-continue path was
    # invisible to admins).

    def _force_rotation_due(self, user):
        """Backdate the user past their rotation TTL.

        Setting `orc.rotation_days = 0` is NOT enough — that was the previous
        approach and it silently disabled these tests. `fields.Datetime.now()`
        is second-granular, so with a 0-day TTL the cron's cutoff equals the
        `orc_last_rotation_at` that setUp's provision just stamped in the same
        second; its predicate is a strict `<`, so the user drops out of `due`,
        the rotate branch never runs, and the assertions below silently read
        setUp's leftover "provisioned on save" / "ok" stamp instead.
        """
        icp = self.env["ir.config_parameter"].sudo()
        rotation_days = int(icp.get_param("orc.rotation_days") or 30)
        user.sudo().write({
            "orc_last_rotation_at": fields.Datetime.subtract(
                fields.Datetime.now(), days=rotation_days + 1,
            ),
        })

    def test_rotate_stamps_ok_on_success(self):
        self._force_rotation_due(self.user)
        calls = {"provision": 0}

        def spy_provision(**kw):
            calls["provision"] += 1
            return "orc-uid-1"

        with patch_orc_client(
            self.env,
            provision_user=spy_provision,
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_rotate_keys()
        self.user.invalidate_recordset()
        # Assert the cron actually rotated. Without this a skipped rotation
        # passes the status check on setUp's leftover "ok" — how the broken
        # `_force_rotation_due` above stayed invisible.
        self.assertEqual(calls["provision"], 1, "expected the cron to rotate the key")
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("rotated", self.user.orc_last_sync_message or "")
        self.assertTrue(self.user.orc_last_sync_at)

    def test_rotate_stamps_error_and_writes_audit_log(self):
        self._force_rotation_due(self.user)

        def fail(**kw):
            raise UserError("ORC down")

        with patch_orc_client(
            self.env,
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

    # ---- orc_gateway_email healing ----------------------------------------

    def _make_bare_login_user(self, login="admin_test_heal"):
        """Create a user with a bare (non-email) login, provision them,
        then clear orc_gateway_email to simulate the pre-fix state."""
        user = self.env["res.users"].create({"name": "Bare Login", "login": login})
        with patch_orc_client(
            self.env,
            provision_user=lambda **kw: "orc-uid-bare",
            push_odoo_key=lambda **kw: None,
        ):
            user.orc_enabled = True
        user.sudo().write({"orc_gateway_email": False})
        user.invalidate_recordset()
        return user

    def test_reconcile_heals_gateway_email_bare_login_form(self):
        """When the gateway still has the user under their bare login
        ('admin'), the reconcile must write that bare login into
        orc_gateway_email so revoke and SSO use the correct identity."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("web.base.url", "https://myco.odoo.com")
        bare = self._make_bare_login_user()
        self.assertFalse(bare.orc_gateway_email)

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user"},
                    {"email": bare.login, "role": "user"},   # bare form
                ],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()

        bare.invalidate_recordset()
        self.assertEqual(bare.orc_gateway_email, bare.login)

    def test_reconcile_heals_gateway_email_qualified_form(self):
        """When the gateway already has the user under their qualified email
        ('admin@myco.odoo.com') — e.g. after a key rotation with the first
        fix commit — the reconcile must find them via the secondary candidate
        and heal orc_gateway_email to the qualified form."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("web.base.url", "https://myco.odoo.com")
        bare = self._make_bare_login_user()
        self.assertFalse(bare.orc_gateway_email)
        qualified = bare._orc_effective_email()   # "admin_test_heal@myco.odoo.com"

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user"},
                    {"email": qualified, "role": "user"},   # qualified form
                ],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()

        bare.invalidate_recordset()
        self.assertEqual(bare.orc_gateway_email, qualified)

    def test_reconcile_qualified_form_does_not_duplicate_provision(self):
        """Regression: a legacy bare-login user whose gateway record lives
        under the qualified email must be healed via that single alias —
        NOT also indexed under the bare login and re-provisioned. The
        pre-fix index registered BOTH aliases, so the bare alias missed
        the remote and fell through to action_orc_provision(), minting a
        second qualified identity for the same local user."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("web.base.url", "https://myco.odoo.com")
        bare = self._make_bare_login_user()
        qualified = bare._orc_effective_email()

        calls = {"provision": 0}

        def spy_provision(**kw):
            calls["provision"] += 1
            return "orc-uid-dup"

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user"},
                    {"email": qualified, "role": "user"},   # qualified only
                ],
                "infrastructures": [],
            },
            provision_user=spy_provision,
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.assertEqual(
            calls["provision"], 0,
            "the bare alias must not trigger a duplicate provision",
        )
        bare.invalidate_recordset()
        self.assertEqual(bare.orc_gateway_email, qualified)
        self.assertEqual(bare.orc_last_sync_status, "ok")

    def test_cron_orc_sync_runs_reconcile(self):
        with patch_orc_client(
            self.env,
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
        with patch_orc_client(
            self.env,
            provision_user=lambda **kw: "orc-uid-1",
            push_odoo_key=lambda **kw: None,
        ):
            self.env["res.users"]._cron_orc_maintenance()
        self.user.invalidate_recordset()
        self.assertEqual(self.user.orc_last_sync_status, "ok")
        self.assertIn("rotated", self.user.orc_last_sync_message or "")


class TestReadOnlyMirror(TransactionCase):
    """The `orc_read_only` cache — pull-refreshed, write-through.

    AI Workplace owns the flag (per-key, gated there on the acting
    human's org-admin permission). Odoo caches it so an admin working here
    can SEE whether a user's AI tools may write, and can CHANGE it without
    leaving the form.

    The invariant is not "Odoo never writes" — it is that Odoo never
    *decides*, and never re-asserts its cached copy. Refresh is pull-only;
    authoring is an explicit call carrying the acting admin, made solely on
    user action. Removing the old Odoo-side read-only gate (18.0.1.6.0) was
    about the posture having ONE author, and a write-through that defers to
    the platform's own permission check preserves exactly that.
    """

    def setUp(self):
        super().setUp()
        share_test_cursor(self)
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("orc.endpoint_url", "https://orc.test")
        icp.set_param("orc.org_token", "orc_test_token")
        icp.set_param("orc.infrastructure_id", "11111111-1111-1111-1111-111111111111")

        self.user = self.env["res.users"].create({
            "name": "Alice Example",
            "login": "alice@acme.test",
        })
        with patch_orc_client(
            self.env,
            provision_user=lambda *a, **kw: "orc-uid-1",
            push_odoo_key=lambda *a, **kw: None,
        ):
            self.user.orc_enabled = True

    def _reconcile(self, remote_user):
        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {"users": [remote_user], "infrastructures": []},
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.user.invalidate_recordset()

    def test_mirrors_read_only_true_from_remote(self):
        self._reconcile({"email": self.user.login, "role": "user", "read_only": True})
        self.assertTrue(self.user.orc_read_only)

    def test_mirrors_read_only_back_to_false(self):
        """An admin allowing writes again in the dashboard must clear the
        mirror on the next tick — a one-way latch would leave the form
        claiming read-only for a user who can now write."""
        self._reconcile({"email": self.user.login, "role": "user", "read_only": True})
        self.assertTrue(self.user.orc_read_only)
        self._reconcile({"email": self.user.login, "role": "user", "read_only": False})
        self.assertFalse(self.user.orc_read_only)

    def test_absent_read_only_key_leaves_the_mirror_untouched(self):
        """Older gateway that predates the field: the response simply has
        no `read_only`. Treat that as unknown and preserve what we have.
        Writing False would assert "this user can write" on no evidence,
        and would flap the value on every tick."""
        self._reconcile({"email": self.user.login, "role": "user", "read_only": True})
        self.assertTrue(self.user.orc_read_only)
        self._reconcile({"email": self.user.login, "role": "user"})
        self.assertTrue(
            self.user.orc_read_only,
            "a response without `read_only` must not overwrite the mirror",
        )

    def test_deprovision_clears_the_mirror(self):
        """A posture only means something while access exists — otherwise
        the form shows 'read-only' beside a user with no AI access."""
        self._reconcile({"email": self.user.login, "role": "user", "read_only": True})
        self.assertTrue(self.user.orc_read_only)
        with patch_orc_client(self.env, revoke_infra_access=lambda **kw: None):
            self.user.orc_enabled = False
        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_read_only)

    def test_flag_never_rides_the_provision_or_reconcile_payloads(self):
        """The clobber-class guard, narrowed but not weakened.

        There is now ONE outbound carrier of this flag — the explicit
        `set_read_only` call from the write-through path. What must stay
        true is that the PERIODIC payloads never carry it: a provision or
        reconcile that asserted a local value would re-create the loop that
        used to clobber dashboard-set state on every tick.
        """
        sent = []
        with patch_orc_client(
            self.env,
            provision_user=lambda *a, **kw: sent.append(kw) or "orc-uid-1",
            push_odoo_key=lambda *a, **kw: sent.append(kw),
            list_users=lambda *a, **kw: {"users": [], "infrastructures": []},
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.assertTrue(sent, "expected the re-provision path to send something")
        for payload in sent:
            self.assertNotIn("read_only", payload)
            self.assertNotIn("orc_read_only", payload)
            self.assertNotIn("access_level", payload)

    def test_reconcile_mirror_refresh_makes_no_outbound_call(self):
        """The most important guard in this file.

        The cron pulls the remote value and stores it. If that local write
        re-entered the write-through path, every tick would post the value
        straight back — the clobber loop, one hop further out. Assert zero
        outbound calls while the mirror is actually changing.
        """
        calls = []

        def refuse(**kw):
            calls.append(kw)

        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user", "read_only": True},
                ],
                "infrastructures": [],
            },
            set_read_only=refuse,
        ):
            self.env["res.users"]._cron_orc_reconcile()

        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_read_only, "mirror should have been refreshed")
        self.assertEqual(calls, [], "the pull refresh must not post anything back")

    def test_deprovision_clear_makes_no_outbound_call(self):
        """Clearing the cache on deprovision is bookkeeping, not intent.

        Posting it would target a credential that was just revoked.
        """
        with patch_orc_client(
            self.env,
            list_users=lambda *a, **kw: {
                "users": [
                    {"email": self.user.login, "role": "user", "read_only": True},
                ],
                "infrastructures": [],
            },
        ):
            self.env["res.users"]._cron_orc_reconcile()
        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_read_only)

        calls = []
        with patch_orc_client(
            self.env,
            revoke_infra_access=lambda **kw: None,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.orc_enabled = False

        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_read_only, "cache should be cleared")
        self.assertEqual(calls, [], "deprovision must not post a posture change")

    def test_editing_the_field_writes_through_as_the_acting_admin(self):
        """User intent DOES go outbound, carrying the acting admin."""
        calls = []
        with patch_orc_client(
            self.env,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.orc_read_only = True

        self.assertEqual(len(calls), 1, "expected exactly one outbound call")
        self.assertTrue(calls[0]["read_only"])
        self.assertEqual(calls[0]["email"], self.user._orc_gateway_identity())
        # The acting admin is the CURRENT user, resolved through the same
        # derivation every other user-scoped call uses.
        self.assertEqual(
            calls[0]["acting_user"],
            self.env.user._orc_gateway_identity(),
        )
        self.user.invalidate_recordset()
        self.assertTrue(self.user.orc_read_only)

    def test_a_refused_write_through_rolls_the_local_value_back(self):
        """AI Workplace refuses (not an org admin there / unreachable).

        The local value must NOT move — otherwise Odoo shows a posture the
        platform never accepted, and the next cron tick silently reverts it.
        """
        def refuse(**kw):
            raise UserError("Only an admin of this key's organization may do that")

        with patch_orc_client(self.env, set_read_only=refuse):
            with self.assertRaises(UserError):
                self.user.orc_read_only = True

        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_read_only)

    def test_writing_the_unchanged_value_makes_no_call(self):
        """A bulk write that merely restates the current value is free."""
        calls = []
        with patch_orc_client(
            self.env,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.write({"orc_read_only": False, "name": "Alice Renamed"})
        self.assertEqual(calls, [])

    def test_enabling_and_setting_read_only_in_one_save_works(self):
        """The ordering fix.

        Ticking access + read-only together is an ordinary save. The target
        has no credential until the enable cascade provisions it, so pushing
        the posture BEFORE that would make AI Workplace 404 and abort the
        whole save — the user could never be enabled this way at all. The
        push must land after provisioning.
        """
        fresh = self.env["res.users"].create({
            "name": "Bob Example",
            "login": "bob@acme.test",
        })
        order = []

        def provision(**kw):
            order.append("provision")
            return "orc-uid-2"

        def push(**kw):
            order.append("set_read_only")

        with patch_orc_client(
            self.env,
            provision_user=provision,
            push_odoo_key=lambda **kw: None,
            set_read_only=push,
        ):
            fresh.write({"orc_enabled": True, "orc_read_only": True})

        self.assertIn("set_read_only", order, "the posture was never pushed")
        self.assertLess(
            order.index("provision"),
            order.index("set_read_only"),
            "the push must run AFTER provisioning, or the credential is absent",
        )
        fresh.invalidate_recordset()
        self.assertTrue(fresh.orc_read_only)

    def test_disabling_in_the_same_save_pushes_nothing(self):
        """No access, no posture — and the credential is being revoked."""
        calls = []
        with patch_orc_client(
            self.env,
            revoke_infra_access=lambda **kw: None,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.write({"orc_enabled": False, "orc_read_only": True})
        self.assertEqual(calls, [])

    def test_setting_it_on_a_disabled_user_pushes_nothing(self):
        with patch_orc_client(self.env, revoke_infra_access=lambda **kw: None):
            self.user.orc_enabled = False
        calls = []
        with patch_orc_client(
            self.env,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.orc_read_only = True
        self.assertEqual(calls, [], "a user with no access has no posture to set")

    def test_create_with_read_only_but_no_access_is_dropped_not_stored(self):
        """`create()` bypasses `write()`.

        Storing the flag without pushing would leave Odoo asserting a
        posture AI Workplace never heard of, which the next mirror refresh
        would silently revert.
        """
        calls = []
        with patch_orc_client(
            self.env,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            created = self.env["res.users"].create({
                "name": "Carol Example",
                "login": "carol@acme.test",
                "orc_read_only": True,
            })
        self.assertEqual(calls, [], "nothing to push for an unprovisioned user")
        self.assertFalse(
            created.orc_read_only,
            "must not store a posture that was never applied remotely",
        )

    def test_create_with_access_does_not_push_because_create_never_provisions(self):
        """`create()` runs no enable cascade — only `write()` does.

        So even `orc_enabled=True` is honoured eventually (reconcile's
        Direction A, next tick), and at creation time there is no credential
        to author against no matter what the flags say. The posture request
        must therefore be dropped AND not stored: a stored-but-unpushed value
        would read as applied and then be silently reverted by the mirror.
        """
        calls = []
        with patch_orc_client(
            self.env,
            provision_user=lambda **kw: "orc-uid-3",
            push_odoo_key=lambda **kw: None,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            created = self.env["res.users"].create({
                "name": "Dave Example",
                "login": "dave@acme.test",
                "orc_enabled": True,
                "orc_read_only": True,
            })
        self.assertEqual(calls, [], "no credential exists at create time")
        created.invalidate_recordset()
        self.assertFalse(
            created.orc_read_only,
            "must not store a posture that was never applied remotely",
        )

    def test_a_suppressed_push_does_not_store_the_value_either(self):
        """The invariant behind two of the review findings.

        Setting the flag on a DISABLED user is suppressed outbound — but if
        the value were still stored, Odoo would show "read-only" beside a
        credential that stays writable, and a later enable would not re-push
        it (that write carries no `orc_read_only` at all). It would simply
        look correct until the mirror quietly reverted it.
        """
        with patch_orc_client(self.env, revoke_infra_access=lambda **kw: None):
            self.user.orc_enabled = False

        calls = []
        with patch_orc_client(
            self.env,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.write({"orc_read_only": True})

        self.assertEqual(calls, [], "no push for a user without access")
        self.user.invalidate_recordset()
        self.assertFalse(
            self.user.orc_read_only,
            "suppressing the push must also suppress the local store",
        )

    def test_a_rename_forced_off_does_not_push_for_the_revoked_identity(self):
        """The login guard forces `orc_enabled` off and deprovisions.

        Eligibility is therefore decided AFTER that guard: a write carrying
        `login` + `orc_read_only` (and no explicit `orc_enabled`) must not
        post a posture for the credential the rename just revoked.
        """
        calls = []
        with patch_orc_client(
            self.env,
            revoke_infra_access=lambda **kw: None,
            set_read_only=lambda **kw: calls.append(kw),
        ):
            self.user.write({
                "login": "alice.renamed@acme.test",
                "orc_read_only": True,
            })

        self.assertEqual(calls, [], "the renamed identity was just revoked")
        self.user.invalidate_recordset()
        self.assertFalse(self.user.orc_enabled, "rename forces access off")
        self.assertFalse(self.user.orc_read_only)

    def test_failed_posture_push_undoes_a_fresh_provision(self):
        """The compensation path — the sharpest failure in this feature.

        `action_orc_provision` grants remote access and commits the Odoo API
        key in its own cursor, so neither is undone by the rollback that a
        failing posture push triggers. Without compensation the admin's
        failed save would leave live READ-WRITE access — the very thing they
        were restricting — and reconcile could not clean it up, because the
        rollback discards the `orc_user_id` breadcrumb Direction B keys off.

        The realistic trigger is not exotic: an Odoo admin who is not an
        organization admin in AI Workplace gets 403 on the push.
        """
        fresh = self.env["res.users"].create({
            "name": "Erin Example",
            "login": "erin@acme.test",
        })
        revoked = []

        def refuse_push(**kw):
            raise UserError("Only an admin of this key's organization may do that")

        with patch_orc_client(
            self.env,
            provision_user=lambda **kw: "orc-uid-4",
            push_odoo_key=lambda **kw: None,
            set_read_only=refuse_push,
            revoke_infra_access=lambda **kw: revoked.append(kw),
        ):
            with self.assertRaises(UserError):
                fresh.write({"orc_enabled": True, "orc_read_only": True})

        self.assertEqual(
            len(revoked), 1,
            "a fresh provision must be revoked when the posture push fails",
        )
        # And the Odoo key must be revoked DURABLY. `action_orc_deprovision`
        # unlinks it in this transaction, which the raise rolls back, so the
        # separately-committed row would otherwise survive unreferenced.
        self.assertFalse(
            fresh._orc_key_exists(fresh.orc_api_key_ref),
            "the committed API key must not survive the compensation",
        )

    def test_failed_posture_push_does_not_revoke_an_existing_enrolment(self):
        """Only THIS write's provisioning is undone.

        An already-enrolled user's access is not ours to revoke over a failed
        posture change — that would turn a refused restriction into an
        outage.
        """
        revoked = []

        def refuse_push(**kw):
            raise UserError("nope")

        with patch_orc_client(
            self.env,
            set_read_only=refuse_push,
            revoke_infra_access=lambda **kw: revoked.append(kw),
        ):
            with self.assertRaises(UserError):
                self.user.orc_read_only = True

        self.assertEqual(revoked, [], "must not revoke a pre-existing enrolment")

