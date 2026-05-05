"""Header-gated API-key authentication.

Verifies:
  * ``X-ORC-Auth`` header is required for the API-key path to engage;
    without it ``_check_credentials`` falls through to password auth.
  * With the header, valid keys authenticate successfully and stamp
    request flags (orc_api_key_authenticated, orc_api_key_readonly).
  * With the header, invalid keys raise AccessDenied AND record a
    ``failed`` row in ``orc.api.access.log``.
  * Expired keys do not authenticate.
"""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessDenied
from odoo.tests import TransactionCase


class _FakeRequest(object):
    def __init__(self, headers=None):
        self.httprequest = MagicMock()
        self.httprequest.headers = headers or {}
        self.httprequest.environ = {
            "REMOTE_ADDR": "10.0.0.1",
            "HTTP_USER_AGENT": "ORC-test/1.0",
        }


@contextmanager
def _simulate_request(headers=None):
    """Patch ``http`` in res_users.py and ir_http access shims."""
    fake = _FakeRequest(headers=headers)
    with patch("odoo.addons.orc_client_provisioning.models.res_users.http") as ru_http, \
         patch("odoo.addons.orc_client_provisioning.models.orc_api_access_log.http") as al_http:
        ru_http.request = fake
        al_http.request = fake
        yield fake


class TestApiKeyAuth(TransactionCase):
    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Bob Example",
            "login": "bob@acme.test",
            "password": "correct horse battery staple",
        })
        # Mint a key for the user using the helper that lives on res.users.
        self.raw_key = self.user._orc_generate_api_key()

    def test_no_header_falls_through_to_password(self):
        """Without X-ORC-Auth, the upstream password check runs."""
        with _simulate_request(headers={}):
            user = self.user.with_user(self.user)
            # Wrong password -> AccessDenied from upstream, not from us.
            with self.assertRaises(AccessDenied):
                user._check_credentials("nope")

    def test_header_with_valid_key_authenticates(self):
        with _simulate_request(headers={"X-ORC-Auth": "1"}) as req:
            user = self.user.with_user(self.user)
            # Should not raise.
            user._check_credentials(self.raw_key)
            self.assertTrue(getattr(req, "orc_api_key_authenticated", False))
            # Default access_level is 'read'.
            self.assertTrue(getattr(req, "orc_api_key_readonly", False))

        # Success row recorded.
        log = self.env["orc.api.access.log"].search(
            [("user_id", "=", self.user.id), ("status", "=", "ok")],
            limit=1,
        )
        self.assertTrue(log)
        self.assertEqual(log.endpoint, "auth")

    def test_header_with_invalid_key_raises_and_logs(self):
        with _simulate_request(headers={"X-ORC-Auth": "1"}):
            user = self.user.with_user(self.user)
            with self.assertRaises(AccessDenied):
                user._check_credentials("0" * 40)

        log = self.env["orc.api.access.log"].search(
            [("user_id", "=", self.user.id), ("status", "=", "failed")],
            limit=1,
        )
        self.assertTrue(log)
        self.assertEqual(log.denial_reason, "invalid-key")

    def test_expired_key_does_not_authenticate(self):
        self.user.sudo().write({
            "orc_api_key_expires_at": fields.Datetime.now() - timedelta(seconds=1),
        })
        with _simulate_request(headers={"X-ORC-Auth": "1"}):
            user = self.user.with_user(self.user)
            with self.assertRaises(AccessDenied):
                user._check_credentials(self.raw_key)

    def test_write_level_key_is_not_readonly(self):
        self.user.sudo().write({"orc_access_level": "write"})
        with _simulate_request(headers={"X-ORC-Auth": "1"}) as req:
            user = self.user.with_user(self.user)
            user._check_credentials(self.raw_key)
            self.assertTrue(getattr(req, "orc_api_key_authenticated", False))
            self.assertFalse(getattr(req, "orc_api_key_readonly", False))

    def test_truncated_candidate_does_not_match(self):
        """A candidate shorter than INDEX_SIZE chars must not slice-match."""
        with _simulate_request(headers={"X-ORC-Auth": "1"}):
            user = self.user.with_user(self.user)
            with self.assertRaises(AccessDenied):
                user._check_credentials(self.raw_key[:4])
