# orc_client_tasks (Phase 2a)

Depends on `orc_client_provisioning`. Adds an in-Odoo entry point to ORC chats:

- Systray icon (overrides Phase-1's icon) shows a popover listing the
  user's ORC tasks (live count of unread). A "+" button creates an
  empty task room and opens a chat window on it in one click; the
  user types their first message inside the chat itself.
- "Open in app" button on the popover header opens the full ORC
  dashboard in a new top-level tab via `/orc/sso/start` (the Phase-1
  SSO flow).
- A foldable Discuss-style chat dock at the bottom-right embeds each
  task as an iframe pointing at `/dashboard/tasks/{room_id}?embed=1`,
  signed in via a one-time SSO nonce minted server-to-server.

## Embedded chat dock — how the iframe is authenticated

Click on a task row → the addon mints a one-time SSO nonce on the
ORC server, the dock JS submits a hidden form `POST /auth/sso?nonce=…`
targeting the iframe's name attribute, ORC consumes the nonce and
sets an iron-session cookie inside the iframe, the iframe follows
the redirect to `/dashboard/tasks/<room_id>?embed=1` already
authenticated.

The cookie is issued with `SameSite=None; Secure; Partitioned`
(CHIPS) so the browser will store it in a cross-site iframe
context. That requires HTTPS — local-HTTP dev installs of ORC
(`ORC_INSECURE_COOKIES=1`) fall back to `SameSite=Lax`, and the
embedded dock won't work in that mode. Use the popover's
"Open in app" link instead during local dev.

The full ORC-side rendering of `/dashboard/tasks/<id>?embed=1`
still uses the desktop layout (sidebar + top bar + multi-column
composer); a dedicated `?embed=compact` view that strips chrome
for a 360×500 dock window is on the roadmap but not required —
the existing layout is usable when the dock is sized larger.

## ORC-side dependencies

- `POST /api/me/tasks` returns the caller's tasks
- `POST /api/tasks/create` creates a new room
- `POST /api/addon/sso-exchange` mints the SSO nonce (with optional
  `return_to` and browser UA/IP forwarded as `X-Browser-*` headers)
- `POST /auth/sso` consumes the nonce and sets the iframe-storable
  `orc_session` cookie (CHIPS-partitioned, SameSite=None+Secure)
- `GET /dashboard/tasks/{id}?embed=1` is the iframe's destination —
  authenticated by the cookie set in the previous step
- A `?embed=compact` variant that strips chrome for tight dock
  windows is on the roadmap but optional; the existing layout
  works for full-height embeds.
