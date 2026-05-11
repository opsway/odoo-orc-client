from urllib.parse import quote

from odoo import api, models


# `dark` / `light` is the contract; anything else gets coerced to
# the default at read time so a fat-fingered admin doesn't silently
# break the embed by forwarding a garbage `?theme=` value.
EMBED_THEME_PARAM = "orc_client_tasks.embed_theme"
EMBED_THEME_DEFAULT = "dark"
EMBED_THEME_VALID = ("dark", "light")


class OrcClientTasksExt(models.AbstractModel):
    """Phase 2a additions to ``orc.client``.

    Phase 1's orc.client covers provisioning + SSO. Phase 2a needs
    three more calls on the same HTTP client:

      - list_my_tasks(acting_user) → the caller's task list
      - create_task(acting_user, infra_id, message) → a new room
      - mint_sso_nonce(..., return_to) → landing a specific task
        (the return_to-aware overload; the Phase 1 signature without
        return_to still works)

    Kept here rather than on the Phase 1 service so the embed flow
    stays inside the Phase 2 addon's ownership.
    """

    _inherit = "orc.client"

    @api.model
    def list_my_tasks(self, *, acting_user: str) -> dict:
        """GET /api/me/tasks on behalf of `acting_user`.

        Returns the raw ORC payload {ok, tasks: [...]}. Each task row
        carries room_id, name, status, infrastructure_name, org_name,
        last_activity — everything the dock needs to render titles and
        the last-activity timestamp that drives the unread badge.
        """
        return self._request(
            "GET",
            "/api/me/tasks",
            acting_user=acting_user,
        )

    @api.model
    def create_task(
        self,
        *,
        acting_user: str,
        infrastructure_id: str,
        message: str = "",
    ) -> dict:
        """POST /api/tasks/create — creates the Matrix room and,
        when ``message`` is non-empty, posts it as the first
        message. Returns ``{ok, room_id}``.

        ``message`` is optional. The "+" popover in the systray
        opens the chat window directly on click; the user types
        their first message inside the chat iframe. The ORC server
        (``orc-app/app/api/tasks/create/route.ts``) already supports
        the no-first-message path and the agent has no "wait for
        first message" guard, so an empty room is a usable starting
        point.
        """
        return self._request(
            "POST",
            "/api/tasks/create",
            acting_user=acting_user,
            json_body={
                "message": message,
                "infrastructure_id": infrastructure_id,
            },
        )

    @api.model
    def mint_sso_nonce(
        self,
        *,
        email: str,
        return_to: str | None = None,
        browser_user_agent: str | None = None,
        browser_ip: str | None = None,
        lang: str | None = None,
    ) -> dict:
        """Phase 2a override that supports the optional return_to.

        Phase 1's implementation didn't pass return_to, so a nonce
        minted via the Phase 1 call would redirect to /dashboard after
        consume. Phase 2a wants to land inside
        ``/dashboard/tasks/{room_id}?embed=1`` for the iframe body,
        which is what return_to carries.

        Browser context (UA + IP) is forwarded via X-Browser-* headers
        the same way Phase 1 does; without it the redeem check would
        false-reject when the user actually clicks through to ORC.

        ``lang`` carries the Odoo user's UI language as a raw Odoo
        locale (``pl_PL``, ``en_US`` …); we normalise to a BCP47
        primary tag (``pl``, ``en``) so the orc-app side doesn't have
        to know Odoo's territory variants. Server-side validates
        against its actual locale catalog and silently drops unknown
        values, so sending ``de`` from a tenant where orc-app doesn't
        ship German messages is harmless.

        Server re-validates the prefix on the exchange and consume
        paths (/dashboard/ only). Passing an invalid path surfaces as
        a UserError raised by ``_request`` (ORC returns 400).
        """
        body: dict = {"email": email}
        if return_to:
            body["return_to"] = return_to
        if lang:
            # "pl_PL" → "pl"; "EN_US" → "en". Defensive lower-case +
            # split because Odoo's lang field is technically free text.
            primary = lang.split("_")[0].strip().lower()
            if primary:
                body["lang"] = primary
        extra = {
            "X-Browser-User-Agent": browser_user_agent,
            "X-Browser-IP": browser_ip,
        }
        return self._request(
            "POST",
            "/api/addon/sso-exchange",
            json_body=body,
            extra_headers={k: v for k, v in extra.items() if v},
        )

    @api.model
    def _build_embed_return_to(self, room_id: str) -> str:
        """Build the iframe `return_to` for a given task room.

        Reads the `orc_client_tasks.embed_theme` config parameter so
        the host Odoo's admin can force `dark` or `light` on the
        embedded chat. The orc-app side picks up `?theme=` and
        toggles the dark class before paint — see
        opsway/odoo-agent-gateway#85.

        The percent-encoding of the room id matches what the
        existing controller emits (`quote(room_id, safe='')`) so
        the iframe's URL stays a single canonical shape.
        """
        encoded = quote(room_id, safe="")
        theme = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(EMBED_THEME_PARAM, EMBED_THEME_DEFAULT)
        )
        if theme not in EMBED_THEME_VALID:
            theme = EMBED_THEME_DEFAULT
        return f"/dashboard/tasks/{encoded}?embed=1&theme={theme}"
