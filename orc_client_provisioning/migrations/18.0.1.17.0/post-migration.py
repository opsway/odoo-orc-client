"""Carry the managed-key ownership pointer over to ``orc_api_key_ref``.

``orc_api_key_id`` was a Many2one onto ``res.users.apikeys``; it is now the
plain Integer ``orc_api_key_ref`` (see the field comment in
``models/res_users.py`` for why the relation was the bug, twice).

Runs as a POST migration on purpose:

* the new column must already exist — the ORM creates it while loading the
  module, i.e. after every pre-migration has run;
* the old column must still exist — core drops it only at the very end of
  the upgrade, when ``ir.model.fields._process_end`` unlinks the metadata
  row of a field no longer defined in Python and its ``unlink()`` calls
  ``_drop_column()``. Post-migrations run before that;
* ``18.0.1.13.0/post-migration.py`` heals dangling pointers on the OLD
  column, and post scripts run in version order, so that heal has already
  happened by the time we copy.

We copy only pointers that still resolve to a live key row: a dangling id
carried over would be harmless now (nothing reads through the id, and
``_orc_key_exists`` reads it as "we own no key") but it would make reconcile
re-provision a user whose key is in fact fine — so drop the ones core's
raw-SQL GC already invalidated. The old column is dropped explicitly rather
than left to ``_process_end``, so the end state does not depend on that
core detail holding.
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
    if not cr.fetchone():
        # Fresh install, or the column was already carried over by a
        # re-run of this script. Nothing to do.
        return

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

    cr.execute("ALTER TABLE res_users DROP COLUMN IF EXISTS orc_api_key_id")

    # Drop the stale field metadata too, so the upgrade leaves no trace of
    # the relation (core would unlink it at _process_end, but its
    # _drop_column() call is then a no-op on the already-dropped column).
    cr.execute(
        """
        DELETE FROM ir_model_fields
         WHERE name = 'orc_api_key_id'
           AND model = 'res.users'
        """
    )

    _logger.info(
        "[orc] carried %s managed-key pointer(s) to orc_api_key_ref; "
        "dropped res_users.orc_api_key_id",
        carried,
    )
