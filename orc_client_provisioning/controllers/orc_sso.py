import logging

import werkzeug.utils
from markupsafe import escape

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class OrcSsoController(http.Controller):

    @http.route("/orc/sso/start", type="http", auth="user", methods=["GET", "POST"], csrf=False)
    def sso_start(self, **_kwargs):
        """Trigger point for the systray "Open ORC" button.

        Flow:
          1. Server-to-server: mint a one-time nonce from ORC.
          2. Return an auto-submitting HTML form that POSTs the nonce
             to ORC's /auth/sso. The nonce never appears in URL query,
             Referer, or browser history.

        Only users with ``orc_enabled = True`` reach this controller;
        the systray component hides the icon for everyone else, but
        we still check server-side to prevent deep-linking.
        """
        user = request.env.user
        if not user.orc_enabled or not user.orc_user_id:
            return request.render(
                "web.http_error",
                {"status_code": 403, "status_message": "Your user is not provisioned in ORC."},
                status=403,
            )

        try:
            data = request.env["orc.client"].sudo().mint_sso_nonce(email=user.login)
        except UserError as exc:
            request.env["orc.audit.log"].sudo().create({
                "user_id": user.id,
                "action": "sso",
                "status": "error",
                "error": str(exc),
            })
            return request.render(
                "web.http_error",
                {"status_code": 502, "status_message": f"ORC handshake failed: {exc}"},
                status=502,
            )

        request.env["orc.audit.log"].sudo().create({
            "user_id": user.id,
            "action": "sso",
            "status": "ok",
        })

        nonce = data.get("nonce")
        url = data.get("url")
        if not nonce or not url:
            return request.render(
                "web.http_error",
                {"status_code": 502, "status_message": "ORC returned an incomplete SSO payload"},
                status=502,
            )

        # Auto-submitting form keeps the nonce in the request body, not
        # the URL. target="_top" breaks out of any iframe the systray
        # might be inside (Odoo studio/form embeds).
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Opening ORC…</title></head>
<body onload="document.forms[0].submit()">
  <form method="POST" action="{escape(url)}" target="_top">
    <input type="hidden" name="nonce" value="{escape(nonce)}">
    <noscript><button type="submit">Continue to ORC</button></noscript>
  </form>
</body></html>"""
        return werkzeug.wrappers.Response(
            response=html,
            status=200,
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
