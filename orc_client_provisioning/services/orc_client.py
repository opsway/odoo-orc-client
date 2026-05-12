from __future__ import annotations

import logging
import secrets

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Default network timeout for every AI Workplace call. Short-ish: these are
# all synchronous admin actions and a hung AI Workplace shouldn't freeze the
# Odoo request worker.
DEFAULT_TIMEOUT = 30


class OrcClientConfig(models.AbstractModel):
    """
    Thin wrapper around ir.config_parameter + requests.

    Every call returns the parsed JSON body on success or raises
    UserError with a human-readable reason. Callers that want to
    swallow the failure must catch UserError themselves.

    AI Workplace auth contract (v3.3+):
      - Server-to-server: `Authorization: Bearer orc_<token>` only.
      - User-scoped: add `X-Acting-User: <email>`; AI Workplace then treats the
        call as "addon acting on behalf of this user" and applies
        that user's org membership + permissions.
    """
    _name = "orc.client"
    _description = "AI Workplace HTTP client (stateless)"

    @api.model
    def _config(self) -> dict:
        icp = self.env["ir.config_parameter"].sudo()
        endpoint = (icp.get_param("orc.endpoint_url") or "").strip().rstrip("/")
        token = (icp.get_param("orc.org_token") or "").strip()
        infra_id = (icp.get_param("orc.infrastructure_id") or "").strip()
        if not endpoint or not token or not infra_id:
            raise UserError(_(
                "AI Workplace is not configured. Set orc.endpoint_url, "
                "orc.org_token and orc.infrastructure_id in System "
                "Parameters before enabling users."
            ))
        return {
            "endpoint": endpoint,
            "token": token,
            "infra_id": infra_id,
        }

    @api.model
    def _request(
        self,
        method: str,
        path: str,
        *,
        acting_user: str | None = None,
        json_body: dict | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        extra_headers: dict | None = None,
    ) -> dict:
        cfg = self._config()
        url = f"{cfg['endpoint']}{path}"
        headers = {
            "Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json",
        }
        if acting_user:
            headers["X-Acting-User"] = acting_user
        if extra_headers:
            for k, v in extra_headers.items():
                if v:
                    headers[k] = v

        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            _logger.warning("AI Workplace %s %s failed: %s", method, path, exc)
            raise UserError(_(
                "Failed to reach AI Workplace at %(url)s: %(err)s"
            ) % {"url": url, "err": exc}) from exc

        if resp.status_code >= 400:
            # AI Workplace always returns JSON even on error; fall back to text.
            try:
                err = resp.json().get("error") or resp.text
            except ValueError:
                err = resp.text
            raise UserError(_(
                "AI Workplace %(method)s %(path)s returned %(code)s: %(err)s"
            ) % {
                "method": method, "path": path,
                "code": resp.status_code, "err": err,
            })

        if not resp.content:
            return {}
        return resp.json()

    # --- High-level operations -------------------------------------------------

    @api.model
    def ping(self) -> bool:
        self._request("GET", "/api/me/orgs")
        return True

    @api.model
    def provision_user(self, *, email: str, name: str, role: str = "user") -> str:
        """Create the user + membership in AI Workplace. Returns user_id.

        Password is random and never shown — the user will only ever
        sign in via SSO handoff. Synapse holds the hash but no login
        path on the Odoo side ever issues it.
        """
        password = secrets.token_urlsafe(32)
        data = self._request(
            "POST",
            "/api/admin/users",
            json_body={"email": email, "name": name, "role": role, "password": password},
        )
        user_id = data.get("user_id")
        if not user_id:
            raise UserError(_("AI Workplace provisioning returned no user_id"))
        return user_id

    @api.model
    def push_odoo_key(
        self,
        *,
        email: str,
        api_key: str,
        odoo_login: str | None = None,
    ) -> dict:
        """Register an Odoo API key for ``email`` against the configured
        infrastructure.

        ``odoo_login`` is the login string Odoo authenticates as. May
        differ from ``email`` (e.g. the Odoo ``admin`` user with email
        ``admin@example.com`` has ``login = "admin"``). When ``None``,
        the AI Workplace defaults to ``email`` — preserves
        pre-refactor behaviour for older deployments that don't pass
        it yet.
        """
        cfg = self._config()
        body = {
            "infrastructure_id": cfg["infra_id"],
            "api_key": api_key,
        }
        if odoo_login is not None:
            body["odoo_login"] = odoo_login
        return self._request(
            "POST",
            "/api/auth/setup-key",
            acting_user=email,
            json_body=body,
        )

    @api.model
    def revoke_infra_access(self, *, email: str) -> None:
        """Revoke this user's access on THIS Odoo instance only.

        Deletes the user's ``user_odoo_keys`` row for the configured
        ``orc.infrastructure_id`` and removes the matching
        ``infrastructure.member`` engine relation. Leaves the user's
        organization membership, their historical task rooms, and
        their enrolments on other Odoos intact — those are not this
        addon's to touch.

        "Leaving the company" / full offboarding is an explicit
        dashboard action on the AI Workplace side; this addon deliberately
        does NOT escalate beyond per-infra revoke.
        """
        cfg = self._config()
        infra_id = cfg["infra_id"]
        self._request(
            "DELETE",
            f"/api/auth/setup-key?infrastructure_id={infra_id}",
            acting_user=email,
        )

    @api.model
    def mint_sso_nonce(
        self,
        *,
        email: str,
        browser_user_agent: str | None = None,
        browser_ip: str | None = None,
    ) -> dict:
        """Mint a one-time SSO nonce for ``email``.

        The browser context (UA and optionally IP) is forwarded to AI Workplace
        via ``X-Browser-User-Agent`` / ``X-Browser-IP``. AI Workplace stamps
        these on the nonce row so the atomic consume at ``/auth/sso``
        can bind on the browser that will actually redeem. Without the
        forward, AI Workplace would record the Odoo server's ``requests``-
        library UA, which never matches a real browser and would turn
        the redeem check into a false-reject.
        """
        extra = {
            "X-Browser-User-Agent": browser_user_agent,
            "X-Browser-IP": browser_ip,
        }
        return self._request(
            "POST",
            "/api/addon/sso-exchange",
            json_body={"email": email},
            extra_headers=extra,
        )

    @api.model
    def list_users(self) -> dict:
        """Reconciliation — returns users with active access on THIS infra.

        Backed by ``/api/addon/infrastructure-users``. The endpoint
        derives the infra from the addon's org-identity Bearer (the
        token is pinned to one ``infrastructure_id``) and filters to
        users who currently hold a key on that infra. A user revoked
        from this Odoo via ``revoke_infra_access`` keeps their org
        membership but loses the per-infra key, so they will NOT
        appear in this response — which is what the reconcile loop
        in ``res.users._cron_orc_reconcile`` relies on for both
        directions of drift detection.

        Response shape: ``{ok, users: [{email, name, role}]}``. The
        legacy ``infrastructures`` field on the org-scoped endpoint
        is intentionally not surfaced here; reconcile only needs the
        per-infra user set.
        """
        return self._request("GET", "/api/addon/infrastructure-users")
