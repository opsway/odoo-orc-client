"""Clear dangling res.users.orc_api_key_id pointers on upgrade.

``orc_api_key_id`` is a Many2one onto ``res.users.apikeys`` declared
``ondelete="set null"``, but that table is ``_auto=False`` so Odoo
never builds a real DB FK — the rule is enforced only by the ORM's
``unlink()``. Odoo core garbage-collects expired keys with a raw-SQL
``DELETE`` (``_gc_user_apikeys``), bypassing the ORM, so the pointer
is left dangling. Reading such a user (e.g. opening the user form)
then raises ``MissingError`` and the form won't render.

On production the autovacuum cron creates these orphans naturally; on
staging/dev the dangling state rides along in the DB dump (crons are
neutralized, so nothing self-heals). Heal both here on upgrade — the
nightly ``_cron_orc_orphan_cleanup`` keeps it clean from then on.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        UPDATE res_users u SET orc_api_key_id = NULL
        WHERE orc_api_key_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM res_users_apikeys k WHERE k.id = u.orc_api_key_id
          )
        """
    )
    if cr.rowcount:
        _logger.info(
            "[orc] cleared %s dangling orc_api_key_id pointer(s) on upgrade",
            cr.rowcount,
        )
