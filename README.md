# odoo-orc-client — 15.0

Odoo 15 addons that connect a client's Odoo instance to the OpsWay ORC
(OpenACP Reasoning Cloud) platform. This is the **15.0** branch; `main`
carries the Odoo 18 line and is where changes land first.

Four addons. The first two are meant to be installed in order:

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

3. **`orc_client_semantic_search`** — permission-aware semantic search
   over `document.page` records, exposed as a single XML-RPC method the
   agent calls as the end user, so Odoo's own `ir.rule` decides what it
   may see. Returns refs only.

4. **`orc_client_build_reporter`** — reports each Odoo.sh build's
   `(sha → build_id, dev URL)` so the developer-flow agent can find the
   right dev server without a GitHub token, and lets a rebuilt staging
   branch reconnect itself to ORC. `auto_install`.

## Odoo 15 and neutralization — read this before relying on staging

Odoo grew per-module neutralization (`odoo.modules.neutralize`, which
executes each addon's `data/neutralize.sql`) in **16.0**. Version 15 has none
of it, so on this branch **neither addon's `neutralize.sql` ever runs**, even
though both files are present and correct.

Left alone, that means a rebuilt Odoo.sh staging branch comes up still holding
the endpoint, Bearer token and infrastructure id of whatever database its dump
came from — normally production. The copy authenticates as production, and
because the parameters are present it never reconnects to a staging ORC
either.

`orc_client_build_reporter` closes this on v15 with `sanitize_if_rebuilt`
(`models/enrollment.py`), which detects a restore by **build identity** rather
than a neutralize flag: credentials are stamped with the build they were
issued to (`orc.bound_build`), and a stamp naming a different build is taken
as proof they arrived in a dump. It clears them, and enrollment then requests
credentials of this build's own.

Three things bound it, and each matters:

- it acts only when `ODOO_STAGE` is explicitly `staging` or `dev` — Odoo.sh
  **production** database names carry a build id too, and it changes on every
  deploy, so identity alone would have production delete its own live
  credentials;
- it deletes only credentials that carry a stamp, so a hand-configured
  instance is never touched — such an instance adopts the current build
  instead, and the rebuild after that sanitizes normally;
- a stamp naming the running build means the dump was restored over itself,
  and the credentials stay.

**Without `orc_client_build_reporter` installed**, a v15 operator must clear
`orc.endpoint_url`, `orc.org_token`, `orc.infrastructure_id`,
`orc.rotation_days` and `orc.enroll_secret` by hand after every staging
rebuild.

## Repository layout (for submodule users)

This repo is designed to be consumed as a git submodule from the main
`odoo-agent-gateway` repo at `./odoo-client/`. Clone with:

```bash
git submodule add git@github.com:opsway/odoo-orc-client.git odoo-client
```

The ORC server side (API endpoints consumed by these addons) lives in
the parent repo. See `../docs/` there for endpoint contracts.

## Requirements

- Odoo 15.0
- OCA `document_page` (from `OCA/knowledge`) — `orc_client_semantic_search`
  indexes `document.page`, the v15 stand-in for what is `knowledge.article`
  on the 18.0 line
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
