"""Shared helpers for the provisioning test suite.

The whole suite mocks ``orc.client`` so the Odoo-side lifecycle (key
create / revoke / row updates / audit log) can be exercised without
hitting the network. Centralised here because patching an Odoo model
correctly is subtle — see ``patch_orc_client``.
"""

from unittest.mock import patch


def share_test_cursor(case):
    """Make ``registry.cursor()`` reuse the running test transaction.

    ``_orc_generate_api_key`` deliberately opens its own cursor and commits,
    so the key is durable and visible to AI Workplace's cross-connection
    probe. Its docstring assumes "Odoo test-mode neutralises
    registry.cursor() commits" — but registry test mode is opt-in and only
    ``HttpCase`` enables it, so in a plain ``TransactionCase`` that cursor is
    a genuinely separate connection. It therefore cannot see the user this
    test created in its still-open transaction, and the key INSERT dies on
    ``res_users_apikeys_user_id_fkey``.

    Entering test mode makes those cursors savepoints on the test's own
    transaction, which restores the assumption the addon code documents (and
    keeps the committed rows out of the database afterwards).

    Odoo renamed the entry point in 19.0, so dispatch on what the test case
    offers rather than on a version number.
    """
    if hasattr(case, "registry_enter_test_mode"):  # 19.0+
        case.registry_enter_test_mode()
    else:  # 18.0
        case.registry.enter_test_mode(case.cr)
        case.addCleanup(case.registry.leave_test_mode)


def patch_orc_client(env, **mocks):
    """Patch ``orc.client`` methods for the duration of a ``with`` block.

    Patches the model **class** rather than the recordset that
    ``env["orc.client"]`` returns. Odoo's ``MetaModel.__new__`` sets
    ``__slots__`` on every model class — the comment in core reads "this
    prevents assignment of non-fields on recordsets" — so a recordset has no
    ``__dict__`` and ``setattr(recordset, "provision_user", ...)`` raises
    ``AttributeError: 'orc.client' object attribute 'provision_user' is
    read-only``. That is what a bare ``patch.multiple(env["orc.client"], ...)``
    does, and it fails on both 18.0 and 19.0. Class attributes are always
    assignable, and every ``orc.client`` method is ``@api.model``, so patching
    the class is equivalent for our purposes.

    Each supplied callable keeps the recordset-free signature used throughout
    the suite (``lambda **kw`` / ``lambda *a, **kw``): the patched function is
    reached as a bound method, so it would otherwise receive the recordset as
    an extra leading positional argument. We drop it here instead of making
    every mock in every test declare it.
    """
    def drop_self(func):
        return lambda _self, *args, **kwargs: func(*args, **kwargs)

    return patch.multiple(
        type(env["orc.client"]),
        **{name: drop_self(func) for name, func in mocks.items()},
    )
