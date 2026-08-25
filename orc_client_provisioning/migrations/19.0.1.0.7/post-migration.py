"""Carry the managed-key ownership pointer over to ``orc_api_key_ref``.

``orc_api_key_id`` was a Many2one onto ``res.users.apikeys``; it is now the
plain Integer ``orc_api_key_ref`` (see the field comment in
``models/res_users.py`` for why the relation was the bug, twice).

**This script performs no DDL, on purpose.** An Odoo.sh build offers neither a
shell nor psql while it runs, so anything that blocks during the upgrade is
indistinguishable from a hang and burns the full ~2 hour build limit before
anyone can look. We therefore keep the upgrade to data and metadata only, and
leave the dead column behind.

Removing a field from Python is normally enough to get an ``ALTER TABLE`` anyway:
core reconciles the leftover metadata in STEP 4 of the loader
(``ir.model.data._process_end``), and ``ir.model.fields.unlink()`` calls
``_drop_column()``. That happens *after* the last per-module commit
(``odoo/modules/loading.py:255``) and nothing commits again until after STEP 9,
so it would hold its lock across the entire ``_register_hook`` loop — strictly
worse than doing it here. Deleting the ``ir_model_fields`` row ourselves means
core finds nothing to unlink, so no ``ALTER TABLE`` runs anywhere in the upgrade.
We delete the matching ``ir_model_data`` xmlid too, so ``_process_end`` has
nothing left to reconcile at all.

The dead column is harmless: nothing in Python references it, and it is nullable.
Drop it out of band, at a quiet moment, on a live instance:

    ALTER TABLE res_users DROP COLUMN IF EXISTS orc_api_key_id;

On the 19.0 line this script has only ever existed in its no-DDL form, so no
instance carries the half-migrated state the 18.0 line had to allow for. A DB
carried over from an 18.0 install that already dropped the column is still safe:
the copy below is guarded on the column existing, and both metadata deletes are
idempotent. Such a DB simply has no dead column to clean up later.

Runs as a POST migration because the new column must already exist, and the ORM
creates it while loading the module — i.e. after every pre-migration.
``19.0.1.0.3/post-migration.py`` heals dangling pointers on the OLD column and
post scripts run in version order, so that heal has already happened by the time
we copy.

We copy only pointers that still resolve to a live key row. A dangling id would
be harmless now (nothing reads through it, and ``_orc_key_exists`` reads it as
"we own no key") but it would make reconcile re-provision a user whose key is in
fact fine, so drop the ones core's raw-SQL GC already invalidated.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'res_users' AND column_name = 'orc_api_key_id'
        """
    )
    carried = 0
    if cr.fetchone():
        cr.execute(
            """
            UPDATE res_users u SET orc_api_key_ref = u.orc_api_key_id
             WHERE u.orc_api_key_id IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM res_users_apikeys k WHERE k.id = u.orc_api_key_id
               )
            """
        )
        carried = cr.rowcount

    # Removing these two rows is what keeps core from issuing the ALTER TABLE
    # (see the module docstring). Both deletes are idempotent.
    cr.execute(
        """
        DELETE FROM ir_model_fields
         WHERE name = 'orc_api_key_id'
           AND model = 'res.users'
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'orc_client_provisioning'
           AND name = 'field_res_users__orc_api_key_id'
        """
    )

    _logger.info(
        "[orc] carried %s managed-key pointer(s) to orc_api_key_ref; "
        "left res_users.orc_api_key_id in place (no DDL during the build)",
        carried,
    )
