"""Unit tests for the Phase 2a additions on ``orc.client``.

The controllers are thin JSON-to-JSON adapters; the interesting logic
lives on the service methods they call. These tests pin the exact
HTTP shape we send to ORC — if ORC renames a field or the Phase 2a
addon starts misrouting, the test fails here instead of at install.

All outbound calls go through ``orc.client._request``, which these
tests patch out so no real network is touched.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase


class TestOrcClientTasksExt(TransactionCase):
    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("orc.endpoint_url", "https://orc.test")
        icp.set_param("orc.org_token", "orc_test_token")
        icp.set_param("orc.infrastructure_id", "11111111-1111-1111-1111-111111111111")

    # ------------------------------------------------------------------ list

    def test_list_my_tasks_hits_me_tasks_with_acting_user(self):
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["acting_user"] = kwargs.get("acting_user")
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "tasks": [{"room_id": "!abc:host"}]}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            data = self.env["orc.client"].list_my_tasks(acting_user="alice@acme.test")

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], "/api/me/tasks")
        self.assertEqual(captured["acting_user"], "alice@acme.test")
        self.assertIsNone(captured["json_body"])
        self.assertEqual(data["tasks"][0]["room_id"], "!abc:host")

    # ------------------------------------------------------------------ create

    def test_create_task_posts_message_and_infra_id(self):
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["acting_user"] = kwargs.get("acting_user")
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "room_id": "!new:host"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            data = self.env["orc.client"].create_task(
                acting_user="alice@acme.test",
                infrastructure_id="22222222-2222-2222-2222-222222222222",
                message="hello agent",
            )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/api/tasks/create")
        self.assertEqual(captured["acting_user"], "alice@acme.test")
        self.assertEqual(
            captured["json_body"],
            {
                "message": "hello agent",
                "infrastructure_id": "22222222-2222-2222-2222-222222222222",
            },
        )
        self.assertEqual(data["room_id"], "!new:host")

    def test_create_task_with_empty_message_still_creates_room(self):
        """Direct-open-chat flow: clicking "+" creates a fresh room
        without an initial message; the user types their first
        message inside the chat iframe. The service must let the
        empty-string `message` through to ORC, which already
        supports the no-first-message path."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "room_id": "!empty:host"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            data = self.env["orc.client"].create_task(
                acting_user="alice@acme.test",
                infrastructure_id="22222222-2222-2222-2222-222222222222",
                message="",
            )

        # Wire shape is symmetric — `message` is still in the body
        # (as an empty string), not silently dropped. Keeps the ORC
        # endpoint contract single-shape regardless of whether the
        # caller seeded a first message or not.
        self.assertEqual(
            captured["json_body"],
            {
                "message": "",
                "infrastructure_id": "22222222-2222-2222-2222-222222222222",
            },
        )
        self.assertEqual(data["room_id"], "!empty:host")

    def test_create_task_message_is_optional(self):
        """The service accepts a call with no `message` kwarg at
        all — same effective behaviour as `message=""`. Keeps the
        addon-side popover JS terse (no need to thread an empty
        string through every call site)."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "room_id": "!omitted:host"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            data = self.env["orc.client"].create_task(
                acting_user="alice@acme.test",
                infrastructure_id="22222222-2222-2222-2222-222222222222",
            )

        self.assertEqual(
            captured["json_body"],
            {
                "message": "",
                "infrastructure_id": "22222222-2222-2222-2222-222222222222",
            },
        )
        self.assertEqual(data["room_id"], "!omitted:host")

    # -------------------------------------------------------------- mint_sso

    def test_mint_sso_nonce_without_return_to_omits_field(self):
        """Phase 1 callers that never passed return_to must keep
        working — the field stays out of the request body entirely."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "nonce": "n1", "url": "https://orc.test/auth/sso"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            self.env["orc.client"].mint_sso_nonce(email="alice@acme.test")

        self.assertEqual(captured["json_body"], {"email": "alice@acme.test"})
        self.assertNotIn("return_to", captured["json_body"])

    def test_mint_sso_nonce_with_return_to_passes_field(self):
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "nonce": "n2", "url": "https://orc.test/auth/sso"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            self.env["orc.client"].mint_sso_nonce(
                email="alice@acme.test",
                return_to="/dashboard/tasks/%21abc%3Ahost?embed=1",
            )

        self.assertEqual(
            captured["json_body"],
            {
                "email": "alice@acme.test",
                "return_to": "/dashboard/tasks/%21abc%3Ahost?embed=1",
            },
        )

    # -------------------------------------------------- mint_sso_nonce (lang)

    def test_mint_sso_nonce_forwards_lang_as_primary_tag(self):
        """Odoo locales come as ``pl_PL``/``en_US``/``de_DE``;
        orc-app's locale list uses BCP47 primary tags (``pl``, ``en``,
        ``de``). Normalise on the addon side so the server doesn't have
        to know Odoo's territory variants."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "nonce": "n3", "url": "https://orc.test/auth/sso"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            self.env["orc.client"].mint_sso_nonce(
                email="alice@acme.test",
                lang="pl_PL",
            )

        self.assertEqual(captured["json_body"].get("lang"), "pl")

    def test_mint_sso_nonce_omits_lang_when_falsy(self):
        """An Odoo user with no ``lang`` set (rare but possible) must
        not poison the request body — orc-app drops unknown values
        anyway, but sending an empty string is noisier than omitting
        the field."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "nonce": "n4", "url": "https://orc.test/auth/sso"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            self.env["orc.client"].mint_sso_nonce(
                email="alice@acme.test",
                lang=None,
            )
            self.env["orc.client"].mint_sso_nonce(
                email="alice@acme.test",
                lang="",
            )
            self.env["orc.client"].mint_sso_nonce(
                email="alice@acme.test",
                lang=False,
            )

        # The last call's body shouldn't include lang.
        self.assertNotIn("lang", captured["json_body"])

    def test_mint_sso_nonce_lowercases_lang(self):
        """Defensive: a future Odoo locale code that comes through
        upper-cased (or with the primary tag already split off but
        capitalised) should land as a plain lower-case primary tag."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "nonce": "n5", "url": "https://orc.test/auth/sso"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            self.env["orc.client"].mint_sso_nonce(
                email="alice@acme.test",
                lang="EN_US",
            )

        self.assertEqual(captured["json_body"].get("lang"), "en")
