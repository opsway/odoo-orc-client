import logging

from odoo import fields, http, models

_logger = logging.getLogger(__name__)

# Upstream `res.users.apikeys` stores a short cleartext slice of each
# key in the `index` column for O(1) row lookup before running the
# (deliberately slow) crypt verify. The constant name has shifted
# across versions — fall back to 5 if the import breaks.
try:
    from odoo.addons.base.models.res_users_apikeys import INDEX_SIZE as _INDEX_SIZE
except Exception:  # pragma: no cover — version safety net
    _INDEX_SIZE = 5


class ResUsersApikeys(models.Model):
    _inherit = "res.users.apikeys"

    orc_access_level = fields.Selection(
        [("read", "Read only"), ("write", "Read / Write")],
        string="ORC access level",
        default="write",
        help=(
            "When 'read', RPC calls authenticated with this key may only "
            "invoke documented read methods (read, search, search_read, "
            "name_search, fields_get, ...). Mutations and any other "
            "method raise AccessError before the body runs, so side "
            "effects (webhooks, mail, external HTTP) never fire."
        ),
    )

    def _check_credentials(self, *, scope, key):
        uid = super()._check_credentials(scope=scope, key=key)
        # Only flag when there is a real request we can stash state on.
        # Cron / test cursors have http.request == None — they shouldn't
        # be restricted anyway; they run as trusted internal code.
        if not uid or not http.request:
            return uid
        # Candidate row lookup. Try both slice conventions so behaviour
        # stays stable if upstream flips prefix<->suffix in a minor rev.
        row = self.env["res.users.apikeys"]
        for idx in (key[-_INDEX_SIZE:], key[:_INDEX_SIZE]):
            row = self.sudo().search(
                [("user_id", "=", uid), ("index", "=", idx)],
                limit=1,
            )
            if row:
                break
        if row and row.orc_access_level == "read":
            http.request.orc_api_key_readonly = True
            _logger.debug(
                "[orc] read-only API key matched for uid=%s key_id=%s",
                uid,
                row.id,
            )
        return uid
