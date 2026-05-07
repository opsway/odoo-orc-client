"""Drop the 13.0.1.x form view that still references orc_access_level.

The 13.0.1.x ``view_users_form_orc`` arch had
``<field name="orc_access_level" ...>``. We removed the field in
13.0.2.0.0 (RW capability split is gone). When Odoo upgrades the
addon, ``security/orc_security.xml`` loads BEFORE
``views/res_users_views.xml`` and writes ``base.group_system``'s
``implied_ids`` to add the ORC manager group. That write triggers
``res.users._update_user_groups_view`` which regenerates
``base.user_groups_view`` and validates the full inheritance tree on
res.users - including our addon's stale form view, still in the DB
with the dropped field reference. Validation fails with
"Field `orc_access_level` does not exist".

Deleting the stale view + its ir_model_data row up-front means the
later ``views/res_users_views.xml`` recreates it with the new arch
in the same upgrade transaction. The addon stays installed; only
the cached arch in the DB is dropped.

Re-runs are safe: the ir_model_data row is gone after the first
successful run, so subsequent migrations no-op.
"""
import logging

_logger = logging.getLogger(__name__)

_STALE_VIEW_XMLIDS = ("view_users_form_orc",)


def migrate(cr, version):
    if version is None:
        return

    cr.execute(
        """
        SELECT d.id, d.res_id, d.name
          FROM ir_model_data d
         WHERE d.module = 'orc_client_provisioning'
           AND d.model = 'ir.ui.view'
           AND d.name = ANY(%s)
        """,
        [list(_STALE_VIEW_XMLIDS)],
    )
    hits = cr.fetchall()
    if not hits:
        _logger.info("[orc] no stale view rows to drop")
        return

    view_ids = [h[1] for h in hits if h[1]]
    data_ids = [h[0] for h in hits]
    names = [h[2] for h in hits]

    if view_ids:
        cr.execute("DELETE FROM ir_ui_view WHERE id = ANY(%s)", [view_ids])
    cr.execute("DELETE FROM ir_model_data WHERE id = ANY(%s)", [data_ids])

    _logger.info(
        "[orc] dropped stale view row(s) for re-creation: %s",
        ", ".join(names),
    )
