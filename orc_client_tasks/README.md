# orc_client_tasks (Phase 2a)

Depends on `orc_client_provisioning`. Adds an in-Odoo entry point to ORC chats:

- Systray icon (overrides Phase-1's icon) shows a popover listing the
  user's ORC tasks (live count of unread, inline new-task composer).
- "Open in app" button on the popover header opens the full ORC
  dashboard in a new top-level tab via `/orc/sso/start` (the Phase-1
  SSO flow).
- A foldable Discuss-style chat dock at the bottom-right embeds each
  task as an iframe pointing at `/dashboard/tasks/{room_id}?embed=1`,
  signed in via a one-time SSO nonce minted server-to-server.

## ⚠️ Embedded chat dock — known limitation (2026-05-07)

The iframe dock is **not viable as the primary experience** for two
independent reasons surfaced during local testing:

### 1. UI was not designed for a small embedded panel

ORC's `/dashboard/tasks/[id]` page renders the full desktop layout —
left sidebar, top bar, multi-column composer. There is no minimized /
compact layout for a 360×500 dock window. Any usable embed would
need an ORC-side `?embed=compact` view that strips chrome to just
the message stream + composer.

### 2. Cross-site cookies blocked in the iframe

ORC's session cookie is currently `SameSite=Lax`, which browsers
refuse to set in a cross-site iframe context. Verified locally —
the cookie is silently dropped, the embed page redirects to ORC's
login form, the user sees a login screen instead of their chat.

Fixing this needs the ORC server to:

- set the session cookie as `SameSite=None; Secure; Partitioned`
  (CHIPS) so it's allowed in a third-party iframe without being
  reusable across embedder origins;
- be served over HTTPS in every environment (including local dev),
  since `Secure` cookies are rejected on plain `http://`;
- add an explicit `frame-ancestors` CSP allow-listing the Odoo
  origin, plus a CSRF token on every state-changing endpoint, since
  `SameSite=None` removes the default CSRF protection the cookie
  used to give for free.

### Decision

Defer the embedded dock until ORC ships an `?embed=compact` view.
Cookie/CHIPS work is only worth doing once the embedded UI itself is
desirable.

In the meantime the popover + "Open in app" flow covers the same
intent without the iframe — see `static/src/js/orc_task_list_popover.js`
and `orc_systray_override.js`. The dock components
(`orc_chat_dock.js`, `orc_chat_window.js`) and `/orc/tasks/open`
controller are kept in the repo so we don't have to rebuild them
when ORC's compact view lands; they just aren't reachable from the UI
right now via task-row clicks unless we re-wire them.

## ORC-side dependencies

- `POST /api/me/tasks` returns the caller's tasks
- `POST /api/tasks/create` creates a new room
- `POST /api/addon/sso-exchange` mints the SSO nonce (with optional
  `return_to` and browser UA/IP forwarded as `X-Browser-*` headers)
- For the deferred embed dock: `GET /dashboard/tasks/{id}?embed=compact`
  (planned), `frame-ancestors` allowlist on the embed page, and the
  CHIPS/HTTPS work above.
