"""Module-load patches that can't be expressed via ``_inherit``.

Odoo 13's RPC dispatch is ``odoo.api.call_kw(model, name, args, kwargs)``
(see ``odoo/api.py:378-389``); it reaches for ``getattr(type(model), name)``
and calls it directly. There is no ``BaseModel._call_kw`` recordset hook,
so an ``_inherit = 'base'`` gate would only catch methods invoked via
plain Python.

We install a thin wrapper on ``odoo.api.call_kw`` so every dispatched
RPC method passes through one chokepoint that:

  1. Records the call in ``orc.api.access.log`` when the request was
     authenticated by an ORC key (header-marked + key-verified upstream
     in ``res.users._check_credentials``).
  2. Enforces the read-only allowlist for keys flagged read-only.

Idempotent: installing twice is a no-op.
"""
import functools
import logging

import odoo.api
from odoo.exceptions import AccessError

from .models.base import (
    READ_ONLY_ALLOWLIST,
    _request_is_orc_authenticated,
    _request_is_readonly,
)

_logger = logging.getLogger(__name__)

_SENTINEL = "_orc_call_kw_gate_installed"

# Captured once at module load. The wrapper below closes over this
# instead of being nested inside ``_install`` so it's reachable for
# unit tests and so the function object's identity is stable across
# any subsequent re-imports.
_ORIGINAL_CALL_KW = getattr(odoo.api, "call_kw", None)


def _orc_call_kw(model, name, args, kwargs):
    """Wrapper installed in place of ``odoo.api.call_kw``.

    Falls through to the original for any request that wasn't
    authenticated by an ORC key. For ORC-authenticated requests, logs
    every call and enforces the read-only allowlist.
    """
    if not _request_is_orc_authenticated():
        return _ORIGINAL_CALL_KW(model, name, args, kwargs)

    env = getattr(model, "env", None)
    denied = _request_is_readonly() and name not in READ_ONLY_ALLOWLIST
    if env is not None:
        try:
            env["orc.api.access.log"].sudo()._record(
                endpoint="%s.%s" % (getattr(model, "_name", "?"), name),
                method=name,
                status="denied" if denied else "ok",
                denial_reason=("read-only key, method %r not allowlisted" % name)
                              if denied else None,
            )
        except Exception:  # pragma: no cover - never break dispatch
            _logger.exception("[orc] failed to record api access log")
    if denied:
        raise AccessError(
            "This API key is read-only and cannot call %r on %s. "
            "Allowed RPC methods: read, search, search_read, "
            "search_count, name_search, fields_get, default_get, "
            "check_access_rights." % (name, getattr(model, "_name", "?"))
        )
    return _ORIGINAL_CALL_KW(model, name, args, kwargs)


setattr(_orc_call_kw, _SENTINEL, True)


def _install():
    if _ORIGINAL_CALL_KW is None:
        _logger.info(
            "[orc] odoo.api.call_kw not found - skipping dispatch gate. "
            "ORM backstop on create/write/unlink still applies."
        )
        return
    if getattr(odoo.api.call_kw, _SENTINEL, False):
        return
    functools.update_wrapper(_orc_call_kw, _ORIGINAL_CALL_KW)
    odoo.api.call_kw = _orc_call_kw
    _logger.debug("[orc] dispatch gate installed on odoo.api.call_kw")


try:
    _install()
except Exception:  # pragma: no cover - never break addon load
    _logger.exception("[orc] failed to install dispatch gate")
