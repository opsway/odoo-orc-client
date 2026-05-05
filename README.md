# odoo-orc-client

Odoo 13 addons that connect a client's Odoo instance to the OpsWay ORC
(OpenACP Reasoning Cloud) platform.

Two addons, meant to be installed in order:

1. **`orc_client_provisioning`** — admin picks which users get ORC
   access; addon auto-creates per-user Odoo API keys (hashed at rest
   on `res.users`), ships them to ORC, rotates on a cron, and adds a
   systray "Open ORC" button that SSOs the user into the ORC
   dashboard without a second login. Authenticates inbound RPC via a
   header-marked path so non-ORC password auth is untouched. Records
   every ORC RPC call and every provisioning event in append-only
   audit logs (immutable at ORM, ACL, and DB-trigger layers).
   **Phase 1, ships first.**

2. **`orc_client_tasks`** — in-Odoo ticket creation UX: Create Ticket
   button in the systray, per-record action-menu entry, exception
   modal override, AUP acceptance embedded in the Create dialog, local
   mirror of tasks with unread counts and sync cron. Depends on
   `orc_client_provisioning`. **Out of scope for the 13.0 port** —
   will be ported once the 18.0 line is complete.

## Repository layout (for submodule users)

This repo is designed to be consumed as a git submodule from the main
`odoo-agent-gateway` repo at `./odoo-client/`. Clone with:

```bash
git submodule add git@github.com:opsway/odoo-orc-client.git odoo-client
```

The ORC server side (API endpoints consumed by these addons) lives in
the parent repo. See `../docs/` there for endpoint contracts.

## Requirements

- Odoo 13.0
- Python 3.6+, `passlib`, `requests` (vendored by Odoo 13)
- PostgreSQL role with `REPLICATION` privilege (the audit-log retention
  cron uses `SET LOCAL session_replication_role = replica` to bypass
  the immutability trigger for its own transaction)
- Outbound HTTPS from the Odoo instance to the ORC endpoint
- ORC-side `odoo-client`-scoped org API token (minted by OpsWay
  super-admin via `orc_api_tokens.scopes @> ARRAY['odoo-client']`)

## Configuration

All configuration lives in `ir.config_parameter` (read restricted to
`base.group_system`). No setup wizard.

| Parameter                       | Required        | Description                                                        |
|---------------------------------|-----------------|--------------------------------------------------------------------|
| `orc.endpoint_url`              | yes             | e.g. `https://orc.opsway.com` (no trailing slash)                  |
| `orc.org_token`                 | yes             | `orc_...` token with scope=odoo-client                             |
| `orc.infrastructure_id`         | yes             | UUID of this Odoo instance in ORC                                  |
| `orc.rotation_days`             | no (default 30) | Odoo API key rotation interval                                     |
| `orc.access_log_retention_days` | no (default 90) | Daily prune cron deletes `orc.api.access.log` rows older than this |

## RPC authentication contract

ORC authenticates against this Odoo instance over XML-RPC / JSON-RPC
using the per-user API key minted by `orc_client_provisioning`. The
inbound request must carry the marker header **and** put the raw key
in the password parameter:

```http
X-ORC-Auth: 1
```

```python
xmlrpc.client.ServerProxy(
    f"{ODOO_URL}/xmlrpc/2/object",
    transport=TransportWithHeader("X-ORC-Auth", "1"),
).execute_kw(db, uid, raw_orc_api_key, model, method, args, kwargs)
```

Without the header, the upstream password-auth path runs unchanged —
internal users keep logging in with passwords, no behaviour changes
for them.

With the header but a bad key, `_check_credentials` records a `failed`
row in `orc.api.access.log` and raises `AccessDenied`; the addon never
falls back to password auth on a header-marked request.

For keys minted with `orc_access_level = 'read'`, every RPC method
not on the read-only allowlist (read, search, search_read,
search_count, name_search, fields_get, default_get,
check_access_rights, …) is rejected before its body runs.

## Audit logs

Both logs are append-only. `write` and `unlink` raise `UserError`, the
ACLs grant only read to the ORC manager group, and a Postgres trigger
on each table raises on UPDATE or DELETE.

| Model                | What it records                                                                                                                                                                                    |
|----------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `orc.audit.log`      | Provisioning lifecycle: `provision`, `rotate`, `deprovision`, `sso`, `reconcile`                                                                                                                   |
| `orc.api.access.log` | Every RPC method dispatched on a header-authenticated request, plus every failed key-auth attempt against a header-marked request (user, login attempted, endpoint, method, status, source IP, UA) |
