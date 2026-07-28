"""Make every DDL in this upgrade fail fast instead of hanging.

Odoo runs the whole module load on one connection, and pre-migrations run
before that load. A session-level ``SET`` therefore stays in force for the rest
of the upgrade — including ``18.0.1.17.0``'s ``ALTER TABLE res_users DROP
COLUMN`` (post scripts run in version order, so it comes after this one) and any
DDL core issues later while verifying extended fields.

Without this, a DDL that cannot get its lock waits forever: an Odoo.sh build has
no shell and no psql, so a lock wait is indistinguishable from a hang and burns
the full ~2 hour build limit before anyone learns anything. 30 seconds is far
more than a fresh build database ever needs — nothing else is connected to it —
so if we do time out, contention is real and the traceback names the statement.

``SET``, not ``SET LOCAL``: ``SET LOCAL`` would be discarded at the first commit
(``odoo/modules/loading.py:255`` commits after every updated module), which is
long before the DDL we are protecting. Because that also means the setting
outlives the upgrade on a pooled connection, this version's **post**-migration
issues the matching ``RESET lock_timeout`` — see the note there.

This is deliberately not a fix for the 2026-07-28 build hang — that hang was in
``_register_hook``, after the loading transaction had already been committed, so
no lock of ours was held. See
``docs/findings/2026-07-28-odoosh-main-build-hang.md`` in the gateway repo. This
is the guardrail that keeps the *next* surprise from costing two hours.
"""

import logging

_logger = logging.getLogger(__name__)

LOCK_TIMEOUT = "30s"


def migrate(cr, version):
    cr.execute("SET lock_timeout = %s", (LOCK_TIMEOUT,))
    _logger.info("[orc] lock_timeout set to %s for the remainder of this upgrade", LOCK_TIMEOUT)
