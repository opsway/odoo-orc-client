import logging
import secrets

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Default network timeout for every ORC call. Short-ish: synchronous
# admin calls; a hung ORC must not freeze an Odoo request worker.
DEFAULT_TIMEOUT = 30

# Stable User-Agent so Cloudflare / WAF rules at the ORC edge can
# whitelist this client without relying on egress-IP allowlisting.
# Keep the literal string ``OpsWayOrcClient`` - operators key bypass
# rules off it.
USER_AGENT = "OpsWayOrcClient/13 (orc_client_provisioning)"


class OrcClientConfig(models.AbstractModel):
    """Stateless wrapper around ir.config_parameter + requests.

    Each public method returns the parsed JSON body on success or
    raises UserError with a human-readable reason. ORC auth contract:
      - Server-to-server: ``Authorization: Bearer orc_<token>`` only.
      - User-scoped: add ``X-Acting-User: <email>``; ORC then treats
        the call as the addon acting on behalf of that user.
    """
    _name = "orc.client"
    _description = "ORC HTTP client (stateless)"

    @api.model
    def _config(self):
        icp = self.env["ir.config_parameter"].sudo()
        endpoint = (icp.get_param("orc.endpoint_url") or "").strip().rstrip("/")
        token = (icp.get_param("orc.org_token") or "").strip()
        infra_id = (icp.get_param("orc.infrastructure_id") or "").strip()
        if not endpoint or not token or not infra_id:
            raise UserError(_(
                "ORC is not configured. Set orc.endpoint_url, "
                "orc.org_token and orc.infrastructure_id in System "
                "Parameters before enabling users."
            ))
        return {"endpoint": endpoint, "token": token, "infra_id": infra_id}

    @api.model
    def _request(
        self,
        method,
        path,
        acting_user=None,
        json_body=None,
        timeout=DEFAULT_TIMEOUT,
    ):
        cfg = self._config()
        url = "%s%s" % (cfg["endpoint"], path)
        headers = {
            "Authorization": "Bearer %s" % cfg["token"],
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if acting_user:
            headers["X-Acting-User"] = acting_user

        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body, timeout=timeout,
            )
        except requests.RequestException as exc:
            _logger.warning("ORC %s %s failed: %s", method, path, exc)
            raise UserError(_(
                "Failed to reach ORC at %(url)s: %(err)s"
            ) % {"url": url, "err": exc}) from exc

        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or resp.text
            except ValueError:
                err = resp.text
            raise UserError(_(
                "ORC %(method)s %(path)s returned %(code)s: %(err)s"
            ) % {
                "method": method, "path": path,
                "code": resp.status_code, "err": err,
            })

        if not resp.content:
            return {}
        return resp.json()

    # --- High-level operations -----------------------------------------------

    @api.model
    def ping(self):
        self._request("GET", "/api/me/orgs")
        return True

    @api.model
    def provision_user(self, email, name, role="member"):
        """Create user + membership in ORC. Returns user_id.

        Password is random and never shown. Users only ever sign in
        via SSO handoff; Synapse holds the hash but no Odoo path
        issues it.

        ORC accepts ``member`` or ``admin`` on this endpoint; admin
        promotion is performed in the ORC dashboard, not from this
        addon, so the addon always passes ``member``.
        """
        password = secrets.token_urlsafe(32)
        data = self._request(
            "POST",
            "/api/admin/users",
            json_body={"email": email, "name": name, "role": role, "password": password},
        )
        user_id = data.get("user_id")
        if not user_id:
            raise UserError(_("ORC provisioning returned no user_id"))
        return user_id

    @api.model
    def push_odoo_key(self, email, api_key, odoo_login=None):
        """Register an Odoo API key for ``email`` against the configured
        infrastructure.

        ``odoo_login`` is the login string Odoo authenticates as. May
        differ from ``email`` (e.g. the built-in ``admin`` user has
        ``login = "admin"`` but a non-matching email). When ``None``,
        ORC defaults to ``email`` server-side - kept as default for
        callers that don't need to disambiguate.
        """
        cfg = self._config()
        body = {
            "infrastructure_id": cfg["infra_id"],
            "api_key": api_key,
        }
        if odoo_login is not None:
            body["odoo_login"] = odoo_login
        self._request(
            "POST",
            "/api/auth/setup-key",
            acting_user=email,
            json_body=body,
        )

    @api.model
    def revoke_infra_access(self, email):
        """Revoke this user's access on THIS Odoo instance only.

        Deletes the user's ``user_odoo_keys`` row for the configured
        ``orc.infrastructure_id`` and removes the matching
        ``infrastructure.member`` engine relation. Leaves the user's
        organization membership and historical task rooms intact -
        full offboarding is a dashboard action.
        """
        cfg = self._config()
        self._request(
            "DELETE",
            "/api/auth/setup-key?infrastructure_id=%s" % cfg["infra_id"],
            acting_user=email,
        )

    @api.model
    def mint_sso_nonce(self, email):
        return self._request(
            "POST",
            "/api/addon/sso-exchange",
            json_body={"email": email},
        )

    @api.model
    def list_users(self):
        """Reconciliation - returns {users, infrastructures} for this org."""
        return self._request("GET", "/api/admin/users")
