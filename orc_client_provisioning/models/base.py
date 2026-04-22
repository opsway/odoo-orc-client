import logging

from odoo import api, http, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


# Pure-read RPC methods we allow when the caller's API key is marked
# read-only. Anything else is rejected before the method body runs —
# that's the whole point: keep webhooks, mail, external HTTP calls and
# raw cr.execute side effects from firing even if they'd rollback.
#
# Kept conservative. Extend carefully, and prefer documenting why a new
# method is safe (i.e. provably performs no writes and no external I/O).
READ_ONLY_ALLOWLIST = frozenset({
    # Core ORM reads
    "read",
    "search",
    "search_read",
    "search_count",
    "search_fetch",
    "exists",
    # Name helpers
    "name_search",
    "name_get",
    # Schema / view helpers (pure metadata)
    "fields_get",
    "default_get",
    "load",
    "load_views",
    "get_views",
    "fields_view_get",
    "get_formview_id",
    "get_formview_action",
    "get_empty_list_help",
    # Access probes (read-only by definition)
    "check_access_rights",
    "check_access_rule",
    "has_access",
    # Grouping (pure read)
    "read_group",
    "web_read_group",
    "web_search_read",
    "web_read",
    "web_name_search",
    # UI helpers that don't persist
    "onchange",
    # Explicit data pull
    "export_data",
})


def _request_is_readonly() -> bool:
    req = http.request
    return bool(req and getattr(req, "orc_api_key_readonly", False))


def _deny(model_name: str, what: str) -> AccessError:
    return AccessError(
        "This API key is read-only and cannot %(what)s on %(model)s. "
        "Allowed RPC methods: read, search, search_read, search_count, "
        "name_search, fields_get, default_get, check_access_rights."
        % {"what": what, "model": model_name}
    )


class Base(models.AbstractModel):
    _inherit = "base"

    def _call_kw(self, name, args, kwargs):
        """RPC gatekeeper.

        Not every Odoo build routes JSON-RPC through ``_call_kw`` — in
        some versions ``odoo.api.call_kw`` dispatches directly on the
        method. For those paths the ORM backstop below (create / write /
        unlink) still blocks persistence; the ``_patches`` module adds a
        second gate at the ``api.call_kw`` level so we catch methods that
        side-effect before they ever write.
        """
        if _request_is_readonly() and name not in READ_ONLY_ALLOWLIST:
            raise _deny(self._name, f"call {name!r}")
        return super()._call_kw(name, args, kwargs)

    @api.model_create_multi
    def create(self, vals_list):
        if _request_is_readonly():
            raise _deny(self._name, "create records")
        return super().create(vals_list)

    def write(self, vals):
        if _request_is_readonly():
            raise _deny(self._name, "modify records")
        return super().write(vals)

    def unlink(self):
        if _request_is_readonly():
            raise _deny(self._name, "delete records")
        return super().unlink()
