"""The published enrollment challenge.

This is the addon family's only ``auth="public"`` route, so it is worth being
precise about what it exposes.

It serves one value: ``sha256(S)``, where ``S`` is a 32-byte random secret this
build generated for itself. That is a COMMITMENT, not a credential. Reading it
grants nothing — proving the challenge needs the preimage, which never leaves
the database and is never served here. The whole scheme exists so that the
public half is safe to publish; if it were not, plain HTTP-01 would have done.

Public on purpose: AI Workplace fetches this before the Odoo has any credential
at all, which is precisely the situation enrollment repairs, so there is
nothing to authenticate with.
"""

from odoo import http
from odoo.http import request

from ..models.enrollment import published_challenge


class OrcEnrollController(http.Controller):

    @http.route(
        "/orc/enroll/challenge",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def challenge(self, **_kwargs):
        """``{"challenge": "<64 lowercase hex>"}``, or 404 when none is pending.

        ``save_session=False`` because anyone who can reach the build can call
        this: an unauthenticated poll must not mint session rows.

        404 rather than an empty body — a build that is not enrolling has
        nothing to say here, and an empty ``challenge`` field would be a shape
        the verifying side has to special-case.

        ``no-store`` because the secret is deleted the moment enrollment
        succeeds; a cached response would keep publishing a commitment for a
        secret that no longer exists.
        """
        challenge = published_challenge(request.env)
        headers = [("Cache-Control", "no-store")]
        if not challenge:
            return request.make_json_response(
                {"error": "no_challenge"}, status=404, headers=headers)
        return request.make_json_response({"challenge": challenge}, headers=headers)
