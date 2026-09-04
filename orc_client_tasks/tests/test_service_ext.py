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

        self.assertEqual(
            captured["json_body"],
            {"odoo_login": "alice@acme.test", "email": "alice@acme.test"},
        )
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
                return_to="/tasks/%21abc%3Ahost?embed=1",
            )

        self.assertEqual(
            captured["json_body"],
            {
                "odoo_login": "alice@acme.test",
                "email": "alice@acme.test",
                "return_to": "/tasks/%21abc%3Ahost?embed=1",
            },
        )

    def test_mint_sso_nonce_sends_uppercase_identity_verbatim_as_odoo_login(self):
        """orc-app lowercases the legacy `email` field before the exact
        users.odoo_login lookup, so an uppercase Odoo login (provisioned
        verbatim, e.g. "Admin@host") would miss. The identity must ride
        in `odoo_login`, passed through case-preserving."""
        captured = {}

        def fake_request(self_, method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"ok": True, "nonce": "n6", "url": "https://orc.test/auth/sso"}

        with patch("odoo.addons.orc_client_provisioning.services.orc_client.OrcClientConfig._request",
                   new=fake_request):
            self.env["orc.client"].mint_sso_nonce(email="Admin@myco.odoo.com")

        self.assertEqual(captured["json_body"]["odoo_login"], "Admin@myco.odoo.com")

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

    # -------------------------------------------------- embed return_to helper
    #
    # The embed iframe URL is built from the room id plus the static
    # `?embed=1` marker, and now also `&theme=` derived from
    # `ir.config_parameter` `orc_client_tasks.embed_theme`. The Odoo
    # admin sets the parameter once under Settings → Technical →
    # Parameters → System Parameters; valid values are `dark` /
    # `light`. The orc-app side reads `?theme=` from the URL and
    # toggles the dark class before paint
    # (see opsway/odoo-agent-gateway#85).

    # The in-source default and the seeded parameter are BOTH `light`,
    # so a test that only ever asserts `light` cannot tell the two apart
    # — nor can it tell either from the coercion fallback. `dark` is the
    # one value only the stored parameter can produce, so it is what
    # pins the read path; patching the module default is what pins the
    # seed. Without those two, deleting the `get_param` call outright
    # would leave this whole block green.
    _THEME_DEFAULT = (
        "odoo.addons.orc_client_tasks.services."
        "orc_client_tasks_ext.EMBED_THEME_DEFAULT"
    )

    def test_embed_return_to_uses_the_seeded_theme(self):
        # The addon seeds `orc_client_tasks.embed_theme` to `light`
        # (data/ir_config_parameter.xml). Force the in-source default to
        # `dark` so only the seeded row can still yield `light` — drop the
        # seed from the manifest and this goes red.
        with patch(self._THEME_DEFAULT, "dark"):
            url = self.env["orc.client"]._build_embed_return_to("!abc:host")
        self.assertEqual(
            url, "/tasks/%21abc%3Ahost?embed=1&theme=light",
        )

    def test_embed_return_to_appends_dark_when_admin_sets_dark(self):
        # `dark` cannot come from the default or the coercion fallback,
        # so this is the test that proves the parameter is read at all.
        self.env["ir.config_parameter"].sudo().set_param(
            "orc_client_tasks.embed_theme", "dark",
        )
        url = self.env["orc.client"]._build_embed_return_to("!abc:host")
        self.assertTrue(url.endswith("&theme=dark"), url)

    def test_embed_return_to_falls_back_when_the_param_is_absent(self):
        # A falsy `set_param` unlinks the row, so this drives the absent
        # path rather than storing the string "False".
        #
        # Deliberately NOT claiming to isolate the `get_param` default
        # argument: it and the coercion fallback are the same constant,
        # so no assertion can separate them. What this pins is that a
        # missing parameter still yields a valid theme.
        self.env["ir.config_parameter"].sudo().set_param(
            "orc_client_tasks.embed_theme", False,
        )
        url = self.env["orc.client"]._build_embed_return_to("!abc:host")
        self.assertTrue(url.endswith("&theme=light"), url)

    def test_embed_return_to_coerces_a_garbage_value_to_the_default(self):
        # Defensive: an admin who fat-fingers `orange` shouldn't
        # send a garbage param to ORC (which would silently leave
        # the SSR cookie default in place — bad UX). Coerce to
        # the documented default, which is `light`.
        self.env["ir.config_parameter"].sudo().set_param(
            "orc_client_tasks.embed_theme", "orange",
        )
        url = self.env["orc.client"]._build_embed_return_to("!abc:host")
        self.assertTrue(url.endswith("&theme=light"), url)

    def test_embed_return_to_percent_encodes_room_id(self):
        # `:` and `!` must be percent-encoded so the path component
        # decodes cleanly on the ORC side.
        url = self.env["orc.client"]._build_embed_return_to("!room:srv")
        self.assertIn("%21room%3Asrv", url)
        self.assertNotIn("!room:srv", url)
