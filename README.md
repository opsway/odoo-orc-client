# odoo-orc-client

Odoo 18 addons that connect a client's Odoo instance to the OpsWay ORC
(OpenACP Reasoning Cloud) platform.

Two addons, meant to be installed in order:

1. **`orc_client_provisioning`** — admin picks which users get ORC
   access; addon auto-creates Odoo API keys, ships them to ORC, rotates
   on a cron, and adds a systray "Open ORC" button that SSOs the user
   into the ORC dashboard without a second login. **Phase 1, ships
   first.**

2. **`orc_client_tasks`** — in-Odoo ticket creation UX: Create Ticket
   button in the systray, per-record action-menu entry, exception
   modal override, AUP acceptance embedded in the Create dialog, local
   mirror of tasks with unread counts and sync cron. Depends on
   `orc_client_provisioning`. **Phase 2, ships after Phase 1 is live.**

## Repository layout (for submodule users)

This repo is designed to be consumed as a git submodule from the main
`odoo-agent-gateway` repo at `./odoo-client/`. Clone with:

```bash
git submodule add git@github.com:opsway/odoo-orc-client.git odoo-client
```

The ORC server side (API endpoints consumed by these addons) lives in
the parent repo. See `../docs/` there for endpoint contracts.

## Requirements

- Odoo 18.0 or later
- Outbound HTTPS from the Odoo instance to the ORC endpoint
- ORC-side `odoo-client`-scoped org API token (minted by OpsWay super-admin
  via `orc_api_tokens.scopes @> ARRAY['odoo-client']`)

## Configuration

All configuration lives in `ir.config_parameter` (read restricted to
`base.group_system`). No setup wizard.

| Parameter | Required | Description |
|---|---|---|
| `orc.endpoint_url` | yes | e.g. `https://orc.opsway.com` (no trailing slash) |
| `orc.org_token` | yes | `orc_...` token with scope=odoo-client |
| `orc.infrastructure_id` | yes | UUID of this Odoo instance in ORC |
| `orc.rotation_days` | no (default 30) | Odoo API key rotation interval |
| `orc.sync_interval_minutes` | no (default 5) | Phase 2 poll cadence |
