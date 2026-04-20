# orc_client_tasks (Phase 2 — stub)

Not yet implemented. `installable=False` until Phase 1 is live in
production and Phase 2 design is ratified against real usage data.

## Planned surface

- `orc.ticket` — local mirror of ORC rooms (room_id, task_id, subject,
  status, unread_count, last_message_at, aup_version, aup_accepted_at,
  related_model, related_res_id, source)
- `orc.aup.acceptance` — per-user × per-version; per-ticket snapshot
- Systray Owl dropdown with unread badge + ticket list
- `Create ORC Ticket` Owl dialog (shared by systray, action menu,
  exception modal)
- Action-menu entry on every form view (pre-fills related record)
- Client-side error-modal override, opt-in per user, scrubs secrets
  before attaching traceback
- Sync cron: `GET /api/me/tasks?updated_since=...` every 5 min

## ORC-side dependencies

- `compliance_acks.source` / `ip` / `user_agent` columns
- `POST /api/tasks/create` accepting `{aup_acceptance: {version, ...}}`
- `GET /api/me/tasks` accepting `?updated_since=<iso>`
- Service bot skipping AUP prompt when `compliance_acks` row exists
  for the current version

Zero new ORC endpoints — all extensions on the existing thin-client
surface.
