# Agent guidance — `orc_client_semantic_search`

This file is for agents working **on** the module. Runtime-agent
guidance (when the ORC agent should use `odoo_semantic_search` vs
`odoo_search_read`) lives in `claude-worker/AGENTS.md` in the ORC
repo, not here.

## What this module is

A self-contained Odoo 18 addon that:
1. Listens to `create`/`write` on configured models.
2. Enqueues a reindex marker per affected record.
3. A cron sweeps the queue, calls the configured embedding provider,
   stores the resulting vector + content hash in `orc.embedding`.
4. Exposes one method, `orc.embedding.semantic_search(query,
   models?, limit?)`, returning `[{model, id, score}]` — refs only.

The full contract is in `README.md`. Read it before making changes.

## Non-negotiables (will be reverted in review)

- **Refs-only response.** `semantic_search` returns `model + id +
  score`. Never titles, snippets, or body excerpts. Single
  permission enforcement layer is the read step downstream.
- **No gateway dependency for embed or search.** This module calls
  the provider directly, configured per-tenant in Odoo Settings.
  Any temptation to "centralize embedding through the ORC gateway"
  has been considered and rejected.
- **No RAG framework imports.** No LlamaIndex, no LangChain. Pure
  Python + numpy + requests + pypdf.
- **No Postgres extensions.** No `pgvector`, no `pg_trgm`, no
  custom system packages. The module must install cleanly on
  Odoo.sh.
- **No legacy fallbacks.** When a config field is renamed or a
  model field changes shape, write a migration. Don't branch on
  "old shape vs new shape" at runtime.

## Workflow when extending the module

The build order for the module itself is **docs → tests →
implementation**. Apply the same order when adding to it:

1. Update `README.md` with the new behaviour first (the contract).
2. Update or add tests under `tests/` against the new contract.
3. Make the tests pass.

## Coding conventions

- **Manifest**: `version` follows Odoo's `18.0.X.Y` pattern, same as
  the sibling addons in this repo.
- **License**: LGPL-3, matching the parent repo.
- **Logger**: one logger per module file via
  `_logger = logging.getLogger(__name__)`.
- **Translations**: user-facing strings in views and exception
  messages go through `_(...)`. Operator-only diagnostic logs
  don't need it.
- **Stored vs computed fields**: prefer stored fields on
  `orc.embedding`. The cron is the only writer; readers should
  never wait on a compute.
- **Indexes**: add SQL `CREATE INDEX` in
  `_auto_init` overrides where Odoo's automatic indexes aren't
  enough — particularly `(model, res_id)` on `orc.embedding`.

## Tests

Run with the standard Odoo test runner. From the host that has the
client's Odoo dev install:

```
odoo-bin -d <test_db> -i orc_client_semantic_search \
  --test-tags=orc_client_semantic_search \
  --stop-after-init
```

Tests must:
- Mock the provider HTTP call (do NOT hit a live OpenAI endpoint
  in CI). Use `unittest.mock.patch` on the provider class's HTTP
  client, not on `requests` directly.
- Use Odoo's `TransactionCase` so each test gets a clean
  transaction.
- Cover both happy paths and provider-error paths.

## Provider abstraction

The module ships with `providers/base.py:EmbeddingProvider` and
`providers/openai.py:OpenAIEmbeddingProvider`. Adding Voyage or any
OpenAI-compatible endpoint should be either:
- A config change (URL + API key swap) for OpenAI-compatible.
- A new provider class (~30 lines) for divergent shapes.

Don't import a vendor SDK; the provider HTTP calls are simple
enough that a `requests.post` is clearer than a 50MB SDK.

## Debugging tips

- Set `daily_token_cap=0` in Settings to pause the cron without
  uninstalling.
- The "Test provider" button issues a single embed of `"ping"` and
  surfaces auth / dimension / latency without writing anything.
- The queue row's `last_error` field carries the most recent
  provider response on each failed attempt — read that before
  digging into logs.
- For development: temporarily set `cron_interval_minutes=1` and
  watch `tail -f` on the Odoo log to see records flow through.

## What lives elsewhere

- The new MCP tool definition (`odoo_semantic_search`) and its
  dispatcher live in **`odoo-mcp/app/main.py`** in the ORC repo,
  not here.
- The activity-line caption ("🔎 Semantic search: …") lives in
  **`gateway/src/hook_renderer.py`** in the ORC repo.
- The runtime-agent prompt guidance ("when to use semantic_search
  vs odoo_search_read") lives in
  **`claude-worker/AGENTS.md`** in the ORC repo.

When making the cross-cutting change, ship the Odoo module first
and verify it works in isolation (operator can call the method via
Odoo's developer-mode RPC console). Then ship the ORC-side wiring
in a separate PR.
