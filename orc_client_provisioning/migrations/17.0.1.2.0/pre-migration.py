"""Delete the three pre-consolidation ir.cron rows.

Before 18.0.1.2.0 the addon shipped three ``noupdate="1"`` cron records:
  * ir_cron_orc_rotate_keys
  * ir_cron_orc_reconcile
  * ir_cron_orc_orphan_cleanup

Data files with ``noupdate="1"`` are NOT refreshed by Odoo on module
upgrade (by design — preserves operator customisations), so replacing
them in ``data/ir_cron.xml`` wouldn't remove the old rows. They'd keep
firing daily alongside the new two crons, running every job twice.

We remove them explicitly here. Operators who had manually edited the
old cron intervals lose those tweaks — acceptable given the overall
reorganisation and the version bump.
"""
import logging

_logger = logging.getLogger(__name__)

_OLD_CRON_XMLIDS = (
    "ir_cron_orc_rotate_keys",
    "ir_cron_orc_reconcile",
    "ir_cron_orc_orphan_cleanup",
)


def migrate(cr, version):
    if version is None:
        # Fresh install — nothing to clean up.
        return

    cr.execute(
        """
        SELECT id, res_id, name
          FROM ir_model_data
         WHERE module = 'orc_client_provisioning'
           AND model  = 'ir.cron'
           AND name   = ANY(%s)
        """,
        [list(_OLD_CRON_XMLIDS)],
    )
    hits = cr.fetchall()
    if not hits:
        _logger.info("[orc] no legacy cron records to purge")
        return

    cron_ids = [h[1] for h in hits if h[1]]
    data_ids = [h[0] for h in hits]
    names = [h[2] for h in hits]

    if cron_ids:
        cr.execute("DELETE FROM ir_cron WHERE id = ANY(%s)", [cron_ids])
    cr.execute("DELETE FROM ir_model_data WHERE id = ANY(%s)", [data_ids])

    _logger.info(
        "[orc] purged %d legacy cron record(s): %s",
        len(hits), ", ".join(names),
    )
