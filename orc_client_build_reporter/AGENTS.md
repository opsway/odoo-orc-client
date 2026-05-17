# AGENTS.md — orc_client_build_reporter

Maintainer guidance for the addon. The user-facing surface lives in
`README.md`.

## What this addon is for, in one paragraph

A daemon thread that fires once per Odoo registry init and reports
`(sha, build_id, stage, dev_url, branch_slug)` to AI Workplace's
public webhook. Workplace stores the mapping so its developer-flow
agent can derive the right SSH target for a given commit.

## Key conventions baked into the code

- **`from odoo.modules.registry import Registry`**, not
  `from odoo import registry`. The lowercase `registry` import path
  was removed in newer Odoo branches; always use the class form.
- **No commits inside the reporter.** `with Registry(dbname).cursor()
  as cr:` relies on the cursor's own commit-on-exit; don't add
  explicit `cr.commit()`.
- **No headers carrying secrets.** The webhook is fully public from
  the addon's perspective — `requests.post` carries only `User-Agent`
  and `Accept`. There is nothing to log-redact.
- **Skip in test mode.** `config.get("test_enable") or
  config.get("test_file")` short-circuits at the top of
  `_run_reporter`. Otherwise running the test suite would post real
  webhook calls.

## Debounce key shape

The ICP key `orc_client_build_reporter.last_report_key` stores
`{sha}:{build_id}:{stage}`. Each component of the triple, when
changed, legitimately re-warrants a fresh report:

- **sha** — new commit.
- **build_id** — same commit, fresh rebuild on the same dev branch
  (or moved to a different environment that allocated a new build).
- **stage** — promotion from dev → staging → production.

Don't drop any of the three — the test
`test_same_sha_new_stage_reposts` pins this behaviour.

## Tests — which class to inherit from

Two test files:

| File | Base class | Why |
|---|---|---|
| `tests/test_helpers.py` | `BaseCase` | Pure functions (parsers, git helper). No DB needed. |
| `tests/test_run_reporter.py` | `TransactionCase` | Reads/writes `ir.config_parameter`; needs the test cursor + savepoint. |

All tests use `@tagged('post_install', '-at_install',
'orc_client_build_reporter')` so they're discoverable by tag and run
after install.

### How `_run_reporter` is tested without committing

`TestRunReporter` patches `reporter.Registry` to return a
`_FakeRegistry` whose `.cursor()` yields the test's `self.env.cr`
and whose `__exit__` is a no-op. This routes every write
`_run_reporter` makes through the test's savepoint — nothing
escapes to the real DB.

`requests.post` is always mocked. `get_commit_sha` is also mocked
(its real behavior is covered separately in `test_helpers.py`) so
this suite doesn't depend on the addon's own checkout state.

### Run the suite

```bash
odoo --test-enable --stop-after-init -i orc_client_build_reporter \
     --addons-path=...,/path/to/odoo-orc-client
# or filter by tag:
odoo --test-enable --stop-after-init \
     --test-tags=orc_client_build_reporter ...
```

The only thing intentionally NOT covered is the live webhook POST —
the assumption is that the addon is exercised in production by
merely existing.

## Compatibility / non-goals

- One repo per Odoo.sh project (1:1). Multi-repo not supported.
- No retries — if the POST fails, the next restart re-attempts. The
  `last_report_key` debounce ensures one post per
  `{sha}:{build_id}:{stage}`.
- Manifest version is `"18.0.1.0.0"` (matches the family's
  `<odoo>.<feature>.<minor>.<patch>` scheme).

## Manual smoke test on a real Odoo.sh build

1. Configure `ORG_ID` + `WEBHOOK_BASE` in `models/build_reporter.py`
   (or set the matching ICP keys) and push.
2. After the build is up:

   ```sql
   -- on AI Workplace's postgres
   SELECT sha, build_id, stage, dev_url, reported_at
     FROM odoo_sh_builds
    WHERE org_id = '<your-org-uuid>'
    ORDER BY reported_at DESC
    LIMIT 5;
   ```

3. To force a re-post on the same SHA (e.g. validating after
   editing), clear the ICP and restart:

   ```python
   env["ir.config_parameter"].sudo().set_param(
       "orc_client_build_reporter.last_report_key", False,
   )
   ```
