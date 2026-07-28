"""Clean up our own removed-field metadata inside the upgrade transaction.

When a field disappears from Python, core reconciles the leftovers in STEP 4 of
the loader (``ir.model.data._process_end``). That step runs *after* the last
per-module commit (``odoo/modules/loading.py:255``) and nothing commits again
until after STEP 9, so those writes sit open across the whole
``_register_hook`` loop. Any addon whose hook takes conflicting locks on the same
rows — and Fiscadoo's is a candidate — blocks there, against a transaction whose
own thread is meanwhile waiting for that hook to return. Postgres cannot see
that as a deadlock: only one side is waiting on a lock.

Doing the cleanup here instead means core finds nothing left to reconcile, so
the loading transaction reaches STEP 9 with no ORC writes outstanding.

Two rows are involved, both from the 1.14 → 1.17 field changes:

* ``field_res_users__orc_api_key_id`` — 1.17.0 deleted the ``ir_model_fields``
  row but not its ``ir_model_data`` xmlid, which is why the production log
  still shows core garbage-collecting it.
* ``selection__res_users__orc_last_sync_status__drift`` — the retired ``drift``
  value. Removing the selection row writes nothing to ``res_users``
  (``ir_model.py:1765`` skips fields that are not ``selection_add`` extensions),
  but its metadata is leftover all the same.

Idempotent, and a no-op on any instance where core already reconciled them
(gourmetfoods staging, which reached 1.17.0 before this shipped).

This is a **reduction of our footprint, not a fix**: if a build still wedges
with nothing of ours outstanding, ORC is excluded and the remaining suspect is
the third-party hook — which is exactly the evidence needed to hand over. See
``docs/findings/2026-07-28-odoosh-main-build-hang.md`` in the gateway repo.
"""

import logging

_logger = logging.getLogger(__name__)

# (module, xmlid name) pairs whose records 1.17.0 removed from Python.
STALE_XMLIDS = [
    ("orc_client_provisioning", "field_res_users__orc_api_key_id"),
    ("orc_client_provisioning", "selection__res_users__orc_last_sync_status__drift"),
]


def migrate(cr, version):
    # The retired selection value, if core has not already dropped it. Deleting
    # the row directly rather than through the ORM is safe precisely because
    # `orc_last_sync_status` is a plain Selection: there is no `ondelete` policy
    # to apply and no data to rewrite.
    cr.execute(
        """
        DELETE FROM ir_model_fields_selection s
         USING ir_model_fields f
         WHERE s.field_id = f.id
           AND f.model = 'res.users'
           AND f.name = 'orc_last_sync_status'
           AND s.value = 'drift'
        """
    )
    selections = cr.rowcount

    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE (module, name) IN %s
        """,
        (tuple(STALE_XMLIDS),),
    )
    xmlids = cr.rowcount

    if selections or xmlids:
        _logger.info(
            "[orc] pre-cleaned removed-field metadata: %s selection row(s), "
            "%s xmlid(s) — nothing left for _process_end to reconcile",
            selections,
            xmlids,
        )

    # Hand the connection back with stock behaviour. The pre-migration's
    # `SET lock_timeout` is session-level, and this connection goes back into
    # Odoo's pool to serve requests and crons — leaving 30s on it would make
    # unrelated later operations fail on ordinary lock contention.
    #
    # Post scripts run in version order, so this lands after 18.0.1.17.0's
    # `ALTER TABLE ... DROP COLUMN`: the guardrail covers every migration in the
    # upgrade, which is the window we control and the one that has bitten us.
    # DDL that core itself issues later in the load (field verification,
    # `_drop_column` for fields removed from Python) is then unprotected again —
    # accepted deliberately, because a timeout leaking into a live worker is a
    # certain harm to every customer, while that residual window is narrow and,
    # for this upgrade, empty: 18.0.1.17.0 already removed the field metadata
    # and the sweep above clears the rest.
    cr.execute("RESET lock_timeout")
