# AI Workplace — Build Reporter

Drop-in Odoo.sh addon. On every registry init (= every Odoo.sh restart), it
POSTs the current build's `(sha, build_id, stage, dev_url)` to your AI
Workplace deployment's public webhook. Workplace stores the mapping so its
developer-flow agent can resolve "given this commit SHA, which dev URL do I
SSH into?" — no GitHub commit-status polling, no PAT in Odoo, no
DB-stored secret.

## Why this exists

Odoo.sh creates a fresh DB on every "New build" mode push. The previous
generation of this addon stored a GitHub PAT in `ir.config_parameter` and
posted a commit status (`ci/odoosh-build-id`) for the agent to read. That
silently broke the moment the PAT got wiped by a New build push — i.e.
roughly every push to a feature branch.

The current shape:

- **No secret in the repo.** `ORG_ID` is a public routing identifier; the SHA
  is public the moment it's pushed.
- **No GitHub token.** Workplace runs its own SHA-on-repo cross-check
  server-side.
- **Survives DB resets.** `ORG_ID` and `WEBHOOK_BASE` live in the addon
  source, which is part of every fresh build's filesystem.
- **Spoof-resistant.** Workplace validates the SHA actually exists on the
  org's known repo, structurally checks the `build_url` is `*.odoo.com`,
  and the agent re-verifies `git rev-parse HEAD` on the dev server before
  acting on the reported `ssh_target`.

## What gets sent

```
POST {WEBHOOK_BASE}/{ORG_ID}/{sha}
{
  "build_url":   "https://<slug>-<build_id>.dev.odoo.com",
  "stage":       "dev" | "staging" | "production",
  "build_id":    "<digits>",
  "branch_slug": "<slug>"
}
```

`sha`, `build_id`, `branch_slug` and `stage` are all auto-derived inside
the build (from `git rev-parse HEAD`, `ODOO_BUILD_URL` / `cr.dbname`, and
the `ODOO_STAGE` env var respectively). The only customer-set values are
`ORG_ID` + `WEBHOOK_BASE`.

## Install

1. Vendor this addon into your Odoo.sh project (typically via
   `git subtree` from `opsway/odoo-orc-client` — see the customer-repo
   `Makefile` for the `make add-subtree` recipe). The addon must be on a
   path Odoo.sh's addons-path scans.
2. Edit `models/build_reporter.py` and set:

   ```python
   ORG_ID = "11111111-2222-3333-4444-555555555555"        # your org's UUID
   WEBHOOK_BASE = "https://orc.example.com/webhook/odoo-sh/build-ready"
   ```

3. Commit and push to your Odoo.sh branch. `auto_install: True` activates
   the addon on the next build.
4. Verify in AI Workplace's PG:

   ```sql
   SELECT sha, build_id, stage, dev_url, reported_at
     FROM odoo_sh_builds
    WHERE org_id = '<your-org-uuid>'
    ORDER BY reported_at DESC
    LIMIT 5;
   ```

### Runtime overrides (optional)

If you can't commit the constants (e.g. validating a staging webhook
without forking), set them via Settings → AI Workplace — Build Reporter,
which is wired to:

| ICP key | Equivalent constant |
|---|---|
| `orc_client_build_reporter.org_id` | `ORG_ID` |
| `orc_client_build_reporter.webhook_base` | `WEBHOOK_BASE` |

ICP wins over the in-source constants. Note that ICP values disappear on
Odoo.sh "New build" pushes — commit the constants for durability.

## Skip conditions

The reporter exits silently (or with a single log line) when:

1. Odoo is in test mode (`test_enable` / `test_file`).
2. No `build_id` can be derived from `ODOO_BUILD_URL` or `cr.dbname` (e.g.
   running locally).
3. The current commit SHA cannot be derived.
4. Neither `ORG_ID` nor `WEBHOOK_BASE` is configured.
5. The current `{sha}:{build_id}:{stage}` triple was already reported
   (`last_report_key` debounce — guards against duplicate posts from
   multiple Odoo workers initialising in parallel).

## Safety

- The reporter runs in a daemon thread wrapped in `try/except Exception`.
  A bug here will never break Odoo startup.
- No PAT, no headers carrying secrets — nothing to leak.
- HTTP timeout is 10s; on timeout the next restart re-attempts.

## Tests

```bash
odoo --test-enable -i orc_client_build_reporter --stop-after-init
# or filter:
odoo --test-enable --stop-after-init --test-tags=orc_client_build_reporter ...
```

The only thing intentionally NOT covered is the live webhook POST.
