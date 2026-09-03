# AGENTS.md — orc_client_build_reporter

Maintainer guidance for the addon. The user-facing surface lives in
`README.md`.

## What this addon is for, in one paragraph

A daemon thread that fires once per Odoo registry init and reports
`(sha, build_id, stage, dev_url, branch_slug, repo)` to AI Workplace's
public webhook. Workplace routes the report to the owning organisation
by matching `repo` against `organizations.github_repo`. The agent then
reads from `odoo_sh_builds` to derive the right SSH target for a given
commit.

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
- **Report the customer repo, not the addon's.** `repo` and `sha`
  come from `get_project_root(addon_dir)`, which climbs out of any
  submodule layer to the outermost working tree. The addon may be
  committed straight into the customer repo *or* pulled in as a
  submodule (`submodules/odoo-orc-client/…`); reading the addon dir's
  own origin/HEAD works only for the former — for a submodule it
  reports `opsway/odoo-orc-client` at the pinned sub-SHA and Workplace
  rejects the webhook. `TestGetProjectRoot` pins both layouts.

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

## Self-enrollment (`models/enrollment.py`)

A second daemon thread off the same `_register_hook`. Where the reporter says
"here is what I am", enrollment says "here is proof I am, please give me a
credential" — so an Odoo.sh staging build that a rebuild neutralized
reconnects itself instead of waiting for an operator.

**This addon now receives a secret.** The "nothing to log-redact" note above
remains true of the *report*; it is no longer true of the module. The minted
token is logged only as its last six characters, and the preimage is never
logged.

### The two invariants that are easy to break

**1. The secret is committed BEFORE the POST** — the opposite of the
reporter's one-transaction claim/post/stamp, and not a style difference. AI
Workplace calls back into this Odoo, onto an arbitrary worker over a different
connection, to read the published challenge. A secret still inside the posting
transaction is invisible there, so holding the transaction across the POST is
green on one worker and dead on Odoo.sh. Same cross-cursor REPEATABLE READ
class as the `18.0.1.13.1` orphaned-key bug.

**2. The secret is claimed once and re-read, never regenerated.** That is what
makes an early commit safe with N workers. If each worker generated its own,
worker A would publish `S_A`, B would overwrite with `S_B`, and A's submission
would fail against a host now serving `sha256(S_B)`. Workers therefore share
one `S`; the race is settled server-side, where the challenge is single-use, so
one worker mints and the rest are told it is already spent — a normal outcome,
not an error.

`test_commits_the_secret_BEFORE_posting` and
`test_a_second_worker_reuses_the_committed_secret` pin these. Both survive a
naive refactor toward "just do it in one transaction like the reporter".

### The proof contract — pinned in prose on purpose

The verifying half lives in `opsway/odoo-agent-gateway` with **no shared
code**, so both sides carry the same three lines:

```
S          64 lowercase hex characters (32 random bytes)
challenge  sha256 of the ASCII BYTES OF THAT HEX STRING, lowercase hex
published  GET /orc/enroll/challenge -> {"challenge": "<64 lowercase hex>"}
```

Hashing the hex **text** rather than the 32 decoded bytes is deliberate: there
is no decoding step for a reimplementation to disagree about.
`test_challenge_is_sha256_of_the_hex_TEXT` asserts both that the text form is
used and that the decoded-bytes form is not.

### Guards, and why each exists

| Guard | Without it |
|---|---|
| `--stop-after-init` / test mode | same build-phase ERROR the reporter's guard prevents |
| database name must split as `<slug>-<build_id>` | a self-hosted Odoo would ask for a token |
| the three `orc.*` params must be missing | a working build re-enrolls on every restart, superseding the token it is using |
| `orc_client_provisioning` must be installed | a real credential minted into parameters nothing reads |
| `enroll_done_key` matches this build | re-enrolling in a loop against whatever is deleting the config, instead of surfacing it |

### `data/neutralize.sql`

Deletes `orc.enroll_secret` and the debounce key. Auto-discovered by
`odoo.modules.neutralize` — it is **not** listed in the manifest's `data`. The
secret is a live proof of ownership; carrying one into a restored copy hands
that copy what it needs to obtain a credential.

### The public route

`/orc/enroll/challenge` is the family's only `auth="public"` route. It serves
one commitment and nothing else, with `save_session=False` so an
unauthenticated poll cannot mint session rows, and `no-store` because the
secret is deleted the moment enrollment succeeds.

## Compatibility / non-goals

- One repo per Odoo.sh project (1:1). Multi-repo not supported.
- The addon may live in the customer repo *or* as a submodule of it;
  both report the customer repo (see `get_project_root`).
- No retries **in the reporter** — if the POST fails, the next restart
  re-attempts. The `last_report_key` debounce ensures one post per
  `{sha}:{build_id}:{stage}`. Enrollment does retry (3 bounded attempts),
  because Workplace calls back seconds after registry init and a frontend
  that is not serving yet would otherwise leave the build unreconnected
  until somebody restarts it by hand — the manual step the feature removes.
- Manifest version follows the family's
  `<odoo>.<feature>.<minor>.<patch>` scheme.

## Manual smoke test on a real Odoo.sh build

1. (Self-hosters only) Configure `WEBHOOK_BASE` in
   `models/build_reporter.py` to point at your Workplace deployment.
2. Push to your dev branch. After the build is up:

   ```sql
   -- on AI Workplace's postgres
   SELECT b.sha, b.build_id, b.stage, b.dev_url, b.reported_at
     FROM odoo_sh_builds b
     JOIN organizations o ON o.id = b.org_id
    WHERE o.github_repo = '<your-org>/<your-repo>'
    ORDER BY b.reported_at DESC
    LIMIT 5;
   ```

3. To force a re-post on the same SHA (e.g. validating after
   editing), clear the ICP and restart:

   ```python
   env["ir.config_parameter"].sudo().set_param(
       "orc_client_build_reporter.last_report_key", False,
   )
   ```

## Manual webhook simulation (no Odoo.sh required)

For local testing of the receiving side without a real build, POST
a synthetic report directly:

```bash
SHA=$(git -C path/to/your/repo rev-parse HEAD)
curl -sS -X POST "https://help.opsway.com/webhook/odoo-sh/build-ready/$SHA" \
     -H "Content-Type: application/json" \
     -d "$(cat <<EOF
{
  "build_url":   "https://acme-32258372.dev.odoo.com",
  "stage":       "dev",
  "build_id":    "32258372",
  "branch_slug": "acme",
  "repo":        "your-org/your-repo"
}
EOF
)"
```
