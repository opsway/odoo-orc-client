"""Module-load patches that can't be expressed via ``_inherit``.

In Odoo 18, JSON-RPC dispatch for ``/web/dataset/call_kw`` goes through
``odoo.api.call_kw`` which calls the bound method *directly* rather than
routing through ``BaseModel._call_kw``. That means an ``_inherit = 'base'``
override of ``_call_kw`` catches XML-RPC and some legacy paths but not
JSON-RPC.

This module installs a thin wrapper around ``odoo.api.call_kw`` so the
read-only allowlist is enforced uniformly. Idempotent — installing twice
is a no-op.
"""
import functools
import logging

import odoo.api
from odoo.exceptions import AccessError

from .models.base import READ_ONLY_ALLOWLIST, _request_is_readonly

_logger = logging.getLogger(__name__)

_SENTINEL = "_orc_readonly_gate_installed"


def _install():
    target = getattr(odoo.api, "call_kw", None)
    if target is None:
        _logger.info(
            "[orc] odoo.api.call_kw not found — skipping dispatch gate. "
            "ORM backstop on create/write/unlink still applies."
        )
        return
    if getattr(target, _SENTINEL, False):
        return

    @functools.wraps(target)
    def call_kw(model, name, *rest, **kw):
        if _request_is_readonly() and name not in READ_ONLY_ALLOWLIST:
            raise AccessError(
                "This API key is read-only and cannot call %r on %s. "
                "Allowed RPC methods: read, search, search_read, "
                "search_count, name_search, fields_get, default_get, "
                "check_access_rights." % (name, getattr(model, "_name", "?"))
            )
        return target(model, name, *rest, **kw)

    setattr(call_kw, _SENTINEL, True)
    odoo.api.call_kw = call_kw
    _logger.debug("[orc] read-only gate installed on odoo.api.call_kw")


try:
    _install()
except Exception:  # pragma: no cover — never break addon load
    _logger.exception("[orc] failed to install read-only gate")
