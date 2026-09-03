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
| `orc.enroll_secret` | never set by hand | written and deleted by self-enrollment; see below |

## Self-enrollment on Odoo.sh staging

Odoo.sh rebuilds a staging branch roughly monthly, and on every "new build"
push. Each rebuild restores a dump and **neutralizes** it, which deletes the four
`orc.*` parameters above — so ORC goes quiet on that environment until somebody
reconnects it by hand. The failure is silent: nothing errors, and it
is usually a person noticing the agent cannot see their staging data.

With `orc_client_build_reporter` installed, a bound staging build reconnects
itself. It publishes a one-time commitment on a public read-only route, proves
it holds the preimage, and writes back the credential ORC mints — then
re-enables the two ORC crons that neutralize switched off.

Nothing happens unless an operator has **armed** that branch on the ORC side
first, so installing the addon does not opt an environment in. Three things
have to be true:

- the database is an Odoo.sh build database (`<branch-slug>-<build-id>`);
- any of the three required `orc.*` parameters is missing (a fully configured
  Odoo never re-enrolls);
- `orc_client_provisioning` is installed — otherwise there is nothing here that
  would read the credential.

**What is exposed publicly:** `GET /orc/enroll/challenge` returns
`{"challenge": "<sha256 hex>"}` while an enrollment is pending, and 404
otherwise. That value is a hash of a random secret, not a credential — reading
it grants nothing, which is the property that makes publishing it safe.

Operators: nothing to configure on a hosted deployment — tail the log for
`[orc_enrollment]` after a rebuild to watch it work.

**Self-hosters** have one more constant than the reporter's `WEBHOOK_BASE`:
`ENROLL_BASE` in `orc_client_build_reporter/models/enrollment.py`, which must
point at your own AI Workplace. `orc.endpoint_url` is derived from it, so
setting it wrong misdirects both the proof and every later API call. The ICP
key `orc_client_build_reporter.enroll_base` overrides it for one-off testing
and is deliberately deleted by neutralize, so it can never ride a dump into a
restored copy.
