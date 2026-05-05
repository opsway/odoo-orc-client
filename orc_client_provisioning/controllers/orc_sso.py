import logging

import werkzeug.wrappers
from markupsafe import escape

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _error_page(status, headline, detail, hint=""):
    """Inline-HTML error response. Dependency-free, themed via inline
    CSS so it works regardless of the host theme.
    """
    hint_html = "<p class='hint'>%s</p>" % escape(hint) if hint else ""
    html = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>%(headline)s</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif;
         background: #f7f7f8; color: #1f2937;
         display: flex; min-height: 100vh; margin: 0;
         align-items: center; justify-content: center; padding: 1rem; }
  .card { background: #fff; border-radius: 12px; max-width: 520px;
          padding: 2rem 2.25rem; box-shadow: 0 10px 30px rgba(0,0,0,.08); }
  h1 { margin: 0 0 .5rem; font-size: 1.35rem; color: #111827; }
  .code { font-size: .85rem; color: #6b7280; margin-bottom: 1rem; }
  .hint { background: #fff8e1; border-left: 3px solid #f59e0b;
          padding: .75rem 1rem; border-radius: 4px; margin: 1rem 0 0;
          font-size: .9rem; color: #78350f; }
  details { margin-top: 1rem; }
  details pre { background: #f3f4f6; padding: .75rem; border-radius: 6px;
                 font-size: .75rem; white-space: pre-wrap; word-break: break-word; }
  a.back { display: inline-block; margin-top: 1.25rem; color: #2563eb;
           text-decoration: none; font-size: .9rem; }
  a.back:hover { text-decoration: underline; }
</style>
</head><body>
  <div class="card">
    <div class="code">ORC * %(status)s</div>
    <h1>%(headline)s</h1>
    %(hint_html)s
    <details><summary>Technical details</summary><pre>%(detail)s</pre></details>
    <a class="back" href="/web">&larr; Back to Odoo</a>
  </div>
</body></html>""" % {
        "status": status,
        "headline": escape(headline),
        "hint_html": hint_html,
        "detail": escape(detail),
    }
    return werkzeug.wrappers.Response(
        response=html,
        status=status,
        content_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _classify_orc_error(message):
    """Turn an ORC HTTP failure into (status, headline, hint)."""
    lower = message.lower()
    if "401" in lower or "token required" in lower or "scope required" in lower:
        return (
            403,
            "ORC isn't accepting this Odoo instance right now",
            "The addon's ORC token was revoked or replaced. Ask a consultant to mint a new one, "
            "then paste it into System Parameters -> orc.org_token.",
        )
    if "403" in lower and "not authorised for this infrastructure" in lower:
        return (
            403,
            "ORC token is pinned to a different environment",
            "Mint a token for this Odoo instance specifically (stage vs prod tokens are not "
            "interchangeable) and update orc.org_token.",
        )
    if "404" in lower and "not provisioned" in lower:
        return (
            404,
            "Your ORC account isn't set up yet",
            "Ask an Odoo admin to tick ORC Enabled on your user, or wait for the next provisioning run.",
        )
    if "failed to reach orc" in lower or "connection" in lower or "timeout" in lower:
        return (
            502,
            "ORC is unreachable",
            "Check that the Odoo server can reach the ORC endpoint (orc.endpoint_url) and try again in a moment.",
        )
    return (502, "ORC handshake failed", "")


class OrcSsoController(http.Controller):
    @http.route("/orc/sso/start", type="http", auth="user", methods=["GET", "POST"], csrf=False)
    def sso_start(self, **_kwargs):
        """Trigger point for the systray "Open ORC" button.

        Flow:
          1. Server-to-server: mint a one-time nonce from ORC.
          2. Return an auto-submitting HTML form that POSTs the nonce
             to ORC's /auth/sso. The nonce never appears in URL query,
             Referer, or browser history.

        Only users with ``orc_enabled = True`` reach the nonce-mint
        path; the systray hides the icon for everyone else, but we
        still check server-side to prevent deep-linking.
        """
        user = request.env.user
        if not user.orc_enabled or not user.orc_user_id:
            return _error_page(
                403,
                "This user isn't provisioned in ORC",
                "Ask an Odoo admin to tick ORC Enabled on your user record.",
            )

        try:
            data = request.env["orc.client"].sudo().mint_sso_nonce(email=user.login)
        except UserError as exc:
            message = str(exc)
            request.env["orc.audit.log"].sudo()._record(
                user_id=user.id,
                action="sso",
                status="error",
                error=message,
            )
            status, headline, hint = _classify_orc_error(message)
            return _error_page(status, headline, message, hint)

        request.env["orc.audit.log"].sudo()._record(
            user_id=user.id,
            action="sso",
            status="ok",
        )

        nonce = data.get("nonce")
        url = data.get("url")
        if not nonce or not url:
            return _error_page(
                502,
                "ORC handshake failed",
                "ORC returned an incomplete SSO payload.",
            )

        # Auto-submitting form keeps the nonce in the body, not the URL.
        # target="_top" breaks out of any iframe (Odoo studio embeds).
        html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Opening ORC...</title></head>
<body onload="document.forms[0].submit()">
  <form method="POST" action="%(url)s" target="_top">
    <input type="hidden" name="nonce" value="%(nonce)s">
    <noscript><button type="submit">Continue to ORC</button></noscript>
  </form>
</body></html>""" % {"url": escape(url), "nonce": escape(nonce)}
        return werkzeug.wrappers.Response(
            response=html,
            status=200,
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
