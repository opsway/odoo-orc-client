from odoo import api, models


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
        message: str,
    ) -> dict:
        """POST /api/tasks/create — creates the Matrix room + first
        message. Returns {ok, room_id}."""
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
    def mint_sso_nonce(self, *, email: str, return_to: str | None = None) -> dict:
        """Phase 2a override that supports the optional return_to.

        Phase 1's implementation didn't pass return_to, so a nonce
        minted via the Phase 1 call would redirect to /dashboard after
        consume. Phase 2a wants to land inside
        ``/dashboard/tasks/{room_id}?embed=1`` for the iframe body,
        which is what return_to carries.

        Server re-validates the prefix on the exchange and consume
        paths (/dashboard/ only). Passing an invalid path surfaces as
        a UserError raised by ``_request`` (ORC returns 400).
        """
        body: dict = {"email": email}
        if return_to:
            body["return_to"] = return_to
        return self._request(
            "POST",
            "/api/addon/sso-exchange",
            json_body=body,
        )
