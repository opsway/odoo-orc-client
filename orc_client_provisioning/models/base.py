"""Request-flag helper for ORC-authenticated calls.

The header-gated key auth in ``res.users._check_credentials`` stashes
``orc_api_key_authenticated`` on ``odoo.http.request`` after a
successful key match. The dispatch wrapper in ``_patches.py`` reads
it via :func:`_request_is_orc_authenticated` to know whether to
record the call in the access log.

The legacy read-only allowlist + ORM backstops were removed in
``13.0.2.0.0`` to align with v18 main: every issued key now has the
user's full Odoo permissions, so there is nothing to gate at the ORM
or dispatch layer beyond logging.
"""
from odoo import http


def _request_is_orc_authenticated():
    """True iff the active request was authenticated with an ORC key.

    The flag is set by ``res.users._check_credentials`` after a
    successful header-gated key verification. Cron / test contexts
    have ``http.request is None`` and never carry this flag.
    """
    req = http.request
    return bool(req and getattr(req, "orc_api_key_authenticated", False))
