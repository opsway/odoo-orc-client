"""Audit-log immutability at three layers.

  * ORM: write/unlink raise UserError on either log model.
  * DB trigger: direct SQL UPDATE/DELETE raises Postgres error.
  * Pruning cron: bypasses the trigger via session_replication_role
    for its own transaction only.
"""
from datetime import timedelta

import psycopg2

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase


class TestOrcAuditLogImmutability(TransactionCase):
    def setUp(self):
        super().setUp()
        self.row = self.env["orc.audit.log"]._record(
            action="provision", status="ok",
        )

    def test_orm_write_raises(self):
        with self.assertRaises(UserError):
            self.row.write({"status": "error"})

    def test_orm_unlink_raises(self):
        with self.assertRaises(UserError):
            self.row.unlink()

    def test_db_trigger_blocks_direct_update(self):
        # Use a savepoint so the integrity error doesn't poison the
        # transaction for downstream tests.
        cr = self.env.cr
        cr.execute("SAVEPOINT trg_update_test")
        try:
            with self.assertRaises(psycopg2.IntegrityError):
                cr.execute(
                    "UPDATE orc_audit_log SET status='ok' WHERE id=%s",
                    [self.row.id],
                )
        finally:
            cr.execute("ROLLBACK TO SAVEPOINT trg_update_test")

    def test_db_trigger_blocks_direct_delete(self):
        cr = self.env.cr
        cr.execute("SAVEPOINT trg_delete_test")
        try:
            with self.assertRaises(psycopg2.IntegrityError):
                cr.execute(
                    "DELETE FROM orc_audit_log WHERE id=%s", [self.row.id],
                )
        finally:
            cr.execute("ROLLBACK TO SAVEPOINT trg_delete_test")


class TestOrcApiAccessLogImmutability(TransactionCase):
    def setUp(self):
        super().setUp()
        self.row = self.env["orc.api.access.log"]._record(
            status="ok", endpoint="res.partner.read", method="read",
        )

    def test_orm_write_raises(self):
        with self.assertRaises(UserError):
            self.row.write({"status": "denied"})

    def test_orm_unlink_raises(self):
        with self.assertRaises(UserError):
            self.row.unlink()

    def test_db_trigger_blocks_direct_update(self):
        cr = self.env.cr
        cr.execute("SAVEPOINT trg_update_access")
        try:
            with self.assertRaises(psycopg2.IntegrityError):
                cr.execute(
                    "UPDATE orc_api_access_log SET status='ok' WHERE id=%s",
                    [self.row.id],
                )
        finally:
            cr.execute("ROLLBACK TO SAVEPOINT trg_update_access")


class TestAccessLogPruneBypass(TransactionCase):
    """The retention cron must be able to delete expired rows despite
    the immutability trigger. It uses ``SET LOCAL session_replication_role
    = replica`` to bypass the trigger only for its own transaction.
    """

    def test_prune_deletes_expired_rows(self):
        # Insert two rows: one ancient, one recent.
        ancient = self.env["orc.api.access.log"]._record(
            status="ok", endpoint="auth", method="_check_credentials",
        )
        recent = self.env["orc.api.access.log"]._record(
            status="ok", endpoint="auth", method="_check_credentials",
        )
        # Backdate the ancient row by direct SQL with the trigger
        # bypassed; otherwise the immutability trigger would block the
        # UPDATE. This sets up the test fixture only.
        cr = self.env.cr
        cr.execute("SAVEPOINT setup_backdate")
        cr.execute("SET LOCAL session_replication_role = replica")
        cr.execute(
            "UPDATE orc_api_access_log SET create_date = %s WHERE id=%s",
            [fields.Datetime.now() - timedelta(days=400), ancient.id],
        )
        cr.execute("RELEASE SAVEPOINT setup_backdate")

        self.env["ir.config_parameter"].sudo().set_param(
            "orc.access_log_retention_days", "90",
        )
        self.env["orc.api.access.log"]._cron_orc_access_log_prune()

        self.assertFalse(
            self.env["orc.api.access.log"].search([("id", "=", ancient.id)])
        )
        self.assertTrue(
            self.env["orc.api.access.log"].search([("id", "=", recent.id)])
        )
