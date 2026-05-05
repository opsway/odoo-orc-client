"""ORM-level read-only enforcement.

The dispatch wrapper in `_patches.py` is the primary gate, but the
ORM-level overrides on ``base`` (create/write/unlink) are belt-and-
braces for any path that bypasses ``odoo.api.call_kw``. We simulate
the request-flag state and verify the backstops fire.
"""
from contextlib import contextmanager
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase

from ..models.base import (
    READ_ONLY_ALLOWLIST,
    _request_is_orc_authenticated,
    _request_is_readonly,
)


class _FakeRequest(object):
    def __init__(self, authenticated=True, readonly=True):
        self.orc_api_key_authenticated = authenticated
        self.orc_api_key_readonly = readonly


@contextmanager
def _simulate(authenticated=True, readonly=True):
    with patch("odoo.addons.orc_client_provisioning.models.base.http") as http_mod:
        http_mod.request = _FakeRequest(authenticated=authenticated, readonly=readonly)
        yield


class TestReadOnlyAllowlist(TransactionCase):
    def test_read_methods_in_allowlist(self):
        for name in ("read", "search", "search_read", "search_count",
                     "name_search", "fields_get", "default_get",
                     "check_access_rights"):
            self.assertIn(name, READ_ONLY_ALLOWLIST, name)

    def test_mutating_methods_not_in_allowlist(self):
        for name in ("create", "write", "unlink", "copy",
                     "action_confirm", "message_post"):
            self.assertNotIn(name, READ_ONLY_ALLOWLIST, name)


class TestReadOnlyOrmBackstop(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Partner = self.env["res.partner"]
        self.existing = self.Partner.create({"name": "Seed"})

    def test_readonly_blocks_create(self):
        with _simulate():
            self.assertTrue(_request_is_orc_authenticated())
            self.assertTrue(_request_is_readonly())
            with self.assertRaises(AccessError):
                self.Partner.create({"name": "Should fail"})

    def test_readonly_blocks_write(self):
        with _simulate():
            with self.assertRaises(AccessError):
                self.existing.write({"name": "Nope"})

    def test_readonly_blocks_unlink(self):
        with _simulate():
            with self.assertRaises(AccessError):
                self.existing.unlink()

    def test_authenticated_but_writable_does_not_block(self):
        with _simulate(authenticated=True, readonly=False):
            p = self.Partner.create({"name": "Writable"})
            p.write({"name": "Renamed"})
            p.unlink()
