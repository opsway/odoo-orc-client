"""Read-only enforcement primitives + ORM backstops.

Three things live here:

1. ``READ_ONLY_ALLOWLIST`` - the set of RPC methods we permit when the
   inbound request was authenticated with a read-only ORC key.
2. ``_request_is_orc_authenticated`` / ``_request_is_readonly`` - tiny
   helpers that read flags stashed on ``odoo.http.request`` by the
   ``res.users._check_credentials`` override.
3. ``Base`` - an ``_inherit = 'base'`` AbstractModel with create/write/
   unlink overrides that act as belt-and-braces against any code path
   that reaches the ORM without going through ``odoo.api.call_kw`` (the
   primary chokepoint, gated by ``_patches.py``).
"""
import logging

from odoo import api, http, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


# Pure-read RPC methods we allow when the caller's API key is marked
# read-only. Trimmed to methods that exist in Odoo 13's base + web; later
# versions added web_read, web_name_search, search_fetch, has_access,
# get_views, get_formview_*, load - those are not on this allowlist.
READ_ONLY_ALLOWLIST = frozenset({
    # Core ORM reads
    "read",
    "search",
    "search_read",
    "search_count",
    "exists",
    # Name helpers
    "name_search",
    "name_get",
    # Schema / view helpers (pure metadata)
    "fields_get",
    "default_get",
    "load_views",
    "fields_view_get",
    "get_empty_list_help",
    # Access probes
    "check_access_rights",
    "check_access_rule",
    # Grouping (pure read)
    "read_group",
    "web_read_group",
    "web_search_read",
    # UI helpers that don't persist
    "onchange",
    # Explicit data pull
    "export_data",
})


def _request_is_orc_authenticated():
    """True iff the active request was authenticated with an ORC key.

    The flag is set by ``res.users._check_credentials`` after a
    successful header-gated key verification. Cron / test contexts have
    ``http.request is None`` and never carry this flag.
    """
    req = http.request
    return bool(req and getattr(req, "orc_api_key_authenticated", False))


def _request_is_readonly():
    """True iff the ORC-authenticated key is the read-only variant."""
    req = http.request
    return bool(req and getattr(req, "orc_api_key_readonly", False))


def _deny(model_name, what):
    return AccessError(
        "This API key is read-only and cannot %(what)s on %(model)s. "
        "Allowed RPC methods: read, search, search_read, search_count, "
        "name_search, fields_get, default_get, check_access_rights."
        % {"what": what, "model": model_name}
    )


class Base(models.AbstractModel):
    _inherit = "base"

    # The dispatch-level gate lives in ``_patches.py`` because Odoo 13
    # has no ``_call_kw`` recordset hook. The ORM backstops below are
    # belt-and-braces for paths that reach the ORM without going through
    # ``odoo.api.call_kw`` (server actions, scheduled code wired to
    # model methods, etc.).

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
