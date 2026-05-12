import json
import logging
from urllib.parse import quote

import werkzeug.wrappers
from markupsafe import escape

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _json_response(payload: dict, status: int = 200) -> werkzeug.wrappers.Response:
    return werkzeug.wrappers.Response(
        response=json.dumps(payload),
        status=status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _not_provisioned() -> werkzeug.wrappers.Response:
    return _json_response(
        {"ok": False, "error": "user_not_provisioned"},
        status=403,
    )


class OrcTasksController(http.Controller):
    """Thin proxies between the Odoo-side OWL chat dock and AI Workplace.

    Three endpoints cover Phase 2a:

      - GET  /orc/tasks/list  → list my AI Workplace tasks (for the systray
        popover + the dock's window-restore path)
      - POST /orc/tasks/open  → mint a one-time SSO nonce with a
        return_to pointing at /dashboard/tasks/{id}?embed=1; the
        browser form-POSTs the nonce into the iframe which lands
        logged-in inside the embed layout
      - POST /orc/tasks/create → create a new task (room + first
        message), returns room_id so the dock can immediately open a
        window on it

    All routes require an Odoo login (``auth="user"``) and refuse
    users whose ``orc_enabled`` flag is False. The AI Workplace-side Bearer
    token lives in ``ir.config_parameter`` and is added to each
    request by ``orc.client.sudo()``.

    Unread counts are deliberately not an endpoint — the browser
    compares ``task.last_activity`` with a localStorage ``last_viewed``
    timestamp per room, which is enough for the MVP badge and avoids
    a DB model whose sole job would be to persist a single timestamp.
    """

    def _guard_user(self):
        user = request.env.user
        if not user.orc_enabled or not user.orc_user_id:
            return None
        return user

    # ------------------------------------------------------------------ list

    @http.route(
        "/orc/tasks/list",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def list_tasks(self, **_kwargs):
        user = self._guard_user()
        if user is None:
            return _not_provisioned()
        try:
            data = (
                request.env["orc.client"]
                .sudo()
                .list_my_tasks(acting_user=user._orc_effective_email())
            )
        except UserError as exc:
            _logger.info("AI Workplace list_my_tasks failed: %s", exc)
            return _json_response(
                {"ok": False, "error": str(exc)},
                status=502,
            )
        return _json_response({"ok": True, "tasks": data.get("tasks", [])})

    # ------------------------------------------------------------------ open

    @http.route(
        "/orc/tasks/open",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def open_task(self, **_kwargs):
        """Mint a nonce targeting /dashboard/tasks/{room_id}?embed=1.

        Body: `room_id` (matrix room id, e.g. !abc:host).

        The browser consumes the returned {url, nonce} by form-POSTing
        into a hidden form inside the iframe — same pattern as the
        Phase 1 systray's "Open AI Workplace" flow, minus the top-level window
        navigation.
        """
        user = self._guard_user()
        if user is None:
            return _not_provisioned()

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except ValueError:
            return _json_response(
                {"ok": False, "error": "invalid_json"},
                status=400,
            )
        room_id = body.get("room_id")
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            # Room IDs always start with `!`; anything else is either
            # an alias (we don't resolve those) or user error.
            return _json_response(
                {"ok": False, "error": "invalid_room_id"},
                status=400,
            )

        # Built by the service so the `?embed=1&theme=<value>` shape
        # stays in one place and the helper-tests cover it.
        # `theme` comes from the `orc_client_tasks.embed_theme`
        # ir.config_parameter (admin-set, defaults to dark).
        return_to = (
            request.env["orc.client"]
            .sudo()
            ._build_embed_return_to(room_id)
        )
        # Forward browser context so AI Workplace's redeem check binds on the
        # browser that will actually consume the nonce — same as Phase-1
        # /orc/sso/start. Without these the redeem false-rejects and the
        # iframe lands on the AI Workplace login screen instead of the embed.
        httpreq = request.httprequest
        browser_ua = httpreq.user_agent.string if httpreq.user_agent else None
        browser_ip = httpreq.remote_addr or None
        # `env.user.lang` is the Odoo user's UI language ("pl_PL",
        # "en_US"…). Pass it raw — `mint_sso_nonce` normalises to a
        # BCP47 primary tag before forwarding to AI Workplace.
        user_lang = user.lang or None
        try:
            data = (
                request.env["orc.client"]
                .sudo()
                .mint_sso_nonce(
                    email=user._orc_effective_email(),
                    return_to=return_to,
                    browser_user_agent=browser_ua,
                    browser_ip=browser_ip,
                    lang=user_lang,
                )
            )
        except UserError as exc:
            _logger.info("AI Workplace mint_sso_nonce failed: %s", exc)
            return _json_response(
                {"ok": False, "error": str(exc)},
                status=502,
            )
        return _json_response({
            "ok": True,
            "nonce": data.get("nonce"),
            "url": data.get("url"),
            "expires_in": data.get("expires_in", 60),
        })

    # ------------------------------------------------------------------ create

    @http.route(
        "/orc/tasks/create",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def create_task(self, **_kwargs):
        """Create an AI Workplace task on behalf of the caller.

        Body: { message: str, infrastructure_id?: str }.
        `infrastructure_id` defaults to ``orc.infrastructure_id`` from
        System Parameters — a single-infra Odoo instance never has to
        send it. Multi-infra pickers (a future addon) can override.
        """
        user = self._guard_user()
        if user is None:
            return _not_provisioned()

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except ValueError:
            return _json_response(
                {"ok": False, "error": "invalid_json"},
                status=400,
            )
        # `message` is optional. The new "+" flow opens the chat
        # window directly and lets the user type their first
        # message inside the iframe; legacy callers (or any future
        # one-shot creation path) can still seed the first message
        # by sending a non-empty string. Empty / missing / non-
        # string all coerce to "" for a single canonical wire shape.
        raw_message = body.get("message")
        message = raw_message.strip() if isinstance(raw_message, str) else ""

        infra_id = body.get("infrastructure_id")
        if not infra_id:
            icp = request.env["ir.config_parameter"].sudo()
            infra_id = (icp.get_param("orc.infrastructure_id") or "").strip()
        if not infra_id:
            return _json_response(
                {"ok": False, "error": "infrastructure_id_missing"},
                status=400,
            )

        try:
            data = (
                request.env["orc.client"]
                .sudo()
                .create_task(
                    acting_user=user._orc_effective_email(),
                    infrastructure_id=infra_id,
                    message=message,
                )
            )
        except UserError as exc:
            _logger.info("AI Workplace create_task failed: %s", exc)
            return _json_response(
                {"ok": False, "error": str(exc)},
                status=502,
            )
        return _json_response({
            "ok": True,
            "room_id": data.get("room_id"),
        })

    # ------------------------------------------------- open-in-orc (full tab)

    @http.route(
        "/orc/tasks/open-in-orc",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def open_in_orc(self, room_id: str | None = None, **_kwargs):
        """Top-level SSO landing on a specific task inside full AI Workplace.

        Mirrors ``/orc/sso/start`` from the provisioning addon but with
        ``return_to`` pointing at ``/dashboard/tasks/{room_id}`` (no
        ``?embed=1``). Used by the chat window's "Open in AI Workplace" link to
        pop the current room in a new tab with the full dashboard chrome.
        """
        user = self._guard_user()
        if user is None:
            return werkzeug.wrappers.Response(
                response=_("Not provisioned in AI Workplace."),
                status=403,
                content_type="text/plain; charset=utf-8",
            )
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            return werkzeug.wrappers.Response(
                response=_("room_id required"),
                status=400,
                content_type="text/plain; charset=utf-8",
            )
        return_to = f"/dashboard/tasks/{quote(room_id, safe='')}"
        try:
            data = (
                request.env["orc.client"]
                .sudo()
                .mint_sso_nonce(email=user._orc_effective_email(), return_to=return_to)
            )
        except UserError as exc:
            _logger.info("AI Workplace mint_sso_nonce (open-in-orc) failed: %s", exc)
            return werkzeug.wrappers.Response(
                response=_("AI Workplace handshake failed: %s") % exc,
                status=502,
                content_type="text/plain; charset=utf-8",
            )

        nonce = data.get("nonce")
        url = data.get("url")
        if not nonce or not url:
            return werkzeug.wrappers.Response(
                response=_("AI Workplace returned an incomplete SSO payload."),
                status=502,
                content_type="text/plain; charset=utf-8",
            )

        # Page title + noscript fallback button are the only end-user
        # visible strings on this redirect page; everything else is
        # auto-submitting JS the user never sees.
        page_title = _("Opening AI Workplace…")
        continue_button = _("Continue to AI Workplace")
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(page_title)}</title></head>
<body onload="document.forms[0].submit()">
  <form method="POST" action="{escape(url)}" target="_top">
    <input type="hidden" name="nonce" value="{escape(nonce)}">
    <noscript><button type="submit">{escape(continue_button)}</button></noscript>
  </form>
</body></html>"""
        return werkzeug.wrappers.Response(
            response=html,
            status=200,
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
