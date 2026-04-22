"""Unit tests for the read-only API-key gate.

We can't easily exercise the real `odoo.api.call_kw` wrapper in a
TransactionCase (no live HTTP request), so we simulate the state the
patched auth layer sets up: mark a fake request with
``orc_api_key_readonly = True`` and verify the ORM + allowlist denies
writes. See ``models/base.py`` for the production code path.
"""
from contextlib import contextmanager
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase

from ..models.base import (
    READ_ONLY_ALLOWLIST,
    _request_is_readonly,
)


class _FakeRequest:
    def __init__(self, readonly: bool):
        self.orc_api_key_readonly = readonly


@contextmanager
def _simulate_readonly(readonly: bool = True):
    """Patch odoo.http.request with a stub that carries our flag."""
    with patch("odoo.addons.orc_client_provisioning.models.base.http") as http_mod:
        http_mod.request = _FakeRequest(readonly)
        yield


class TestReadOnlyKeyEnforcement(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Partner = self.env["res.partner"]
        self.existing = self.Partner.create({"name": "Seed"})

    # --- allowlist membership ------------------------------------------------

    def test_read_methods_are_in_allowlist(self):
        for name in ("read", "search", "search_read", "search_count",
                     "name_search", "fields_get", "default_get",
                     "check_access_rights"):
            self.assertIn(name, READ_ONLY_ALLOWLIST, name)

    def test_mutating_methods_not_in_allowlist(self):
        for name in ("create", "write", "unlink", "copy",
                     "action_confirm", "message_post"):
            self.assertNotIn(name, READ_ONLY_ALLOWLIST, name)

    # --- ORM backstop --------------------------------------------------------

    def test_readonly_flag_blocks_create(self):
        with _simulate_readonly(True):
            self.assertTrue(_request_is_readonly())
            with self.assertRaises(AccessError):
                self.Partner.create({"name": "Should fail"})

    def test_readonly_flag_blocks_write(self):
        with _simulate_readonly(True):
            with self.assertRaises(AccessError):
                self.existing.write({"name": "Nope"})

    def test_readonly_flag_blocks_unlink(self):
        with _simulate_readonly(True):
            with self.assertRaises(AccessError):
                self.existing.unlink()

    # --- dispatch gatekeeper (the `_call_kw` path) ---------------------------

    def test_call_kw_blocks_non_allowlisted_method(self):
        # `copy` is a write operation; it must be denied.
        with _simulate_readonly(True):
            with self.assertRaises(AccessError):
                self.existing._call_kw("copy", [], {})

    def test_call_kw_allows_read_methods(self):
        with _simulate_readonly(True):
            # search is on the allowlist — should pass the gate.
            result = self.Partner._call_kw("search", [[]], {"limit": 1})
            self.assertTrue(hasattr(result, "ids"))

    # --- write-level key is unaffected ---------------------------------------

    def test_write_level_does_not_block(self):
        with _simulate_readonly(False):
            self.assertFalse(_request_is_readonly())
            p = self.Partner.create({"name": "Writable"})
            p.write({"name": "Renamed"})
            p.unlink()
            # No assertion raised = pass.


class TestApiKeyAccessLevelField(TransactionCase):
    """Smoke test: the field exists and defaults correctly."""

    def test_field_defaults_to_write(self):
        user = self.env["res.users"].create({
            "name": "Bob Example",
            "login": "bob@acme.test",
        })
        raw = self.env["res.users.apikeys"].with_user(user).sudo()._generate(
            scope="rpc", name="test-key", expiration_date=False,
        )
        self.assertTrue(raw)
        row = self.env["res.users.apikeys"].sudo().search(
            [("user_id", "=", user.id), ("name", "=", "test-key")],
            limit=1,
        )
        self.assertEqual(row.orc_access_level, "write")
