# `orc_client_semantic_search`

Permission-aware semantic search over Odoo records, exposed to the
ORC agent via a single XML-RPC method.

The agent today finds Odoo records with hand-built keyword domains
(`name ilike "..."`). That breaks on paraphrase: a user asking
"how do we bill prepayments?" never reaches an article titled
"Down-payment workflow". This module fixes the recall problem by
indexing record text with vector embeddings, while leaving
permissions where they already work — Odoo's `ir.rule` filters every
read by the agent's principal user.

## How it fits

- **This module** holds embeddings + runs the search inside the
  tenant's Odoo. It calls the embedding provider directly; the ORC
  gateway is not in the embed path.
- **`odoo-mcp`** (in the ORC repo) gains one new typed tool,
  `odoo_semantic_search`, that proxies to this module's
  `orc.embedding.semantic_search()` method.
- **The agent** uses semantic_search to locate candidate records,
  then calls the existing typed reads (`odoo_read_knowledge_article`,
  `odoo_read_attachment`, …) **as the end user**. Odoo's record
  rules silently drop forbidden ids.

If this module isn't installed, the tool simply errors and the agent
falls back to keyword search. No cross-tenant coordination.

## Permission model

The search method returns refs only — `[{model, id, score}]`. No
titles, no snippets, no body. The blob field, if exposed, is raw
float vectors — useless without the embedding model.

There is **one** layer of permission enforcement: the agent's read
step, which uses the end user's API key and runs through Odoo's
`ir.rule` machinery exactly as before. The `orc.embedding` rows
themselves are gated to the system + technical group via standard
Odoo ACL — random portal users cannot list rows directly, but even
if they could, the rows tell them nothing useful.

This means we deliberately do **not** replicate Odoo's dynamic
record rules in the index. Adding model B to the indexed set is a
config-row change with zero ACL implications.

## Data model

### `orc.embedding` (one row per indexed record)

| Field | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `model` | char(64) | `knowledge.article`, … |
| `res_id` | int | Record id within `model` |
| `vector_blob` | binary | `numpy.tobytes()` of a float32 array; sized by `vector_dim` from config |
| `content_hash` | char(64) | sha256 of the extracted text used to build the vector |
| `text_excerpt_len` | int | Length of the text fed to the embedder (for diagnostics) |
| `indexed_at` | datetime | Last time the vector was (re)computed |
| `provider` | char(64) | The provider id at indexing time (e.g. `openai:text-embedding-3-small`) |

Constraint: `UNIQUE (model, res_id)`. Group ACL: `base.group_system`
read/write; no portal access.

### `orc.embedding.config` (singleton + per-model toggles)

Two purposes in one model. The singleton row holds provider
credentials; one row per indexed model holds the per-model toggle
and field selection.

| Field | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `is_global` | bool | True for the singleton; False for per-model rows |
| `provider_kind` | selection | `openai`, `voyage`, `openai_compat` (only on global row) |
| `provider_url` | char | Defaults to `https://api.openai.com/v1/embeddings` (only on global row) |
| `provider_api_key` | char | Encrypted via Odoo's password-style char (only on global row) |
| `provider_model` | char | e.g. `text-embedding-3-small` (only on global row) |
| `vector_dim` | int | e.g. 1536. Must match `provider_model` (only on global row) |
| `cron_interval_minutes` | int | Default 5 (only on global row) |
| `daily_token_cap` | int | Hard upper bound on tokens-per-day; cron pauses on overrun (only on global row) |
| `model_name` | char | The Odoo model to index (only on per-model rows) |
| `enabled` | bool | Whether to index this model (only on per-model rows) |
| `text_field_path` | char | Dotted path to the text source. `body` for `knowledge.article`. Future models may use `description` or `name + body` (only on per-model rows) |
| `text_extractor` | selection | `html_strip` (default for HTML fields), `plain` (no transform), `attachment` (run pypdf etc.) — only on per-model rows |

Singleton enforcement: a unique constraint on `is_global=True` (only
one global row may exist). Per-model rows must have
`is_global=False` and a unique `model_name`.

### `orc.embedding.queue` (pending reindex markers)

| Field | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `model` | char | Same as `orc.embedding.model` |
| `res_id` | int | |
| `enqueued_at` | datetime | Set when the marker is created |
| `attempts` | int | Incremented on each cron pass; cron skips after 5 with a warning |
| `last_error` | text | Provider error from the most recent failed attempt |

Constraint: `UNIQUE (model, res_id)`. The cron upserts on
re-enqueue (a second write to an already-queued record doesn't add
a row).

## Configuration UI

Settings → Technical → AI Semantic Search. The page has three
sections.

### 1. Provider (the singleton row)

- **Provider kind** — radio: OpenAI / Voyage / OpenAI-compatible.
- **Endpoint URL** — defaults to OpenAI's URL; user changes for
  Voyage, on-prem llama.cpp, or any compat-layer.
- **API key** — password-style char, stored encrypted.
- **Model** — text input. e.g. `text-embedding-3-small`.
- **Vector dim** — int. Must match the chosen model. Validated at
  save time by issuing a test embed of the string `"ping"` and
  checking the dimensionality.
- **Cron interval (minutes)** — int. Default 5.
- **Daily token cap** — int. Default 1,000,000. Cron checks against
  today's accumulated `text_excerpt_len` sum and pauses on overrun.

### 2. Indexed models (per-model rows)

A list view + form. v1 ships one row pre-configured:

| model_name | enabled | text_field_path | text_extractor |
|---|---|---|---|
| `knowledge.article` | True | `body` | `html_strip` |

Adding a model is data-driven: create a new row, set
`text_field_path`, save. The cron picks it up on the next pass.

### 3. Index status

- **Total records indexed** — count of `orc.embedding` rows.
- **Pending re-index** — count of `orc.embedding.queue` rows.
- **Errors in the last hour** — count of queue rows with non-empty
  `last_error` and `enqueued_at >= now - 1h`.
- **"Reindex all"** button — clears the relevant rows and enqueues
  every record of every enabled model. Operator-only confirmation
  modal because of the cost implication.
- **"Test provider"** button — issues a single embed of `"ping"`
  and reports success / dimensionality / latency.

## API surface

One method, on the `orc.embedding` model:

```python
@api.model
def semantic_search(self, query, models=None, limit=10):
    """
    Returns a list of refs ranked by cosine similarity to `query`.

    :param query: str — natural-language query. Embedded in-line
        via the configured provider.
    :param models: list[str] | None — restrict to these Odoo models.
        Defaults to all `orc.embedding.config` rows with
        `enabled=True`.
    :param limit: int — top-K. Default 10. Max 50.

    :returns: list[dict] — [{"model": str, "id": int, "score": float}, ...]
        Sorted descending by score, score in [0, 1] (cosine on
        L2-normalised vectors). NO titles, snippets, or body —
        callers must read records via the standard Odoo APIs as
        the end user, where `ir.rule` enforces visibility.

    :raises UserError: when the provider call fails, the global
        config is missing, or no enabled models match the request.
    """
```

Callers (the gateway, the agent via odoo-mcp) authenticate via the
end user's Odoo API key, exactly like any other XML-RPC call.

## Indexing lifecycle

```
[Odoo create/write on indexed model]
            │
            ▼
   write hook → upsert orc.embedding.queue row
            │
   ┌────────▼────────┐
   │   ir.cron job   │  every N min (config)
   └────────┬────────┘
            │  for each queue row:
            │    1. read source record
            │    2. extract text per text_extractor
            │    3. hash; if matches existing orc.embedding.content_hash → drop queue row, no-op
            │    4. else: call provider.embed(text)
            │    5. upsert orc.embedding row, drop queue row
            │  on provider error:
            │    bump attempts, store last_error, leave queue row in place
            │    (skip after 5 attempts with a warning log)
            │
            ▼
[orc.embedding row updated, queue row gone]
```

When a record is **deleted**, the corresponding `orc.embedding`
row is removed via an `unlink` hook (cascade by `(model, res_id)`).
When a record is **archived**, the embedding stays (the agent will
never see it because reads filter on `active=True` by default).

## Agent integration

`odoo-mcp` registers one new typed tool, `odoo_semantic_search`,
near the existing typed CRUD tools. It dispatches to a fresh
handler that calls `orc.embedding.semantic_search()` over XML-RPC
and returns the refs straight through.

`gateway/src/hook_renderer.py` adds an activity caption matching
the existing pattern:

```
🔎 Semantic search: <truncated query>…
```

`claude-worker/AGENTS.md` adds a short section on tool selection:

- **Use `odoo_semantic_search`** when the user asks an open-ended
  question and you don't know which records contain the answer.
  The result is candidates, not authoritative — read the top 2–3
  with the relevant typed read before answering.
- **Use `odoo_search_read` with keyword domains** when you have a
  verbatim string to match (Jira key, product code, exact partner
  name, "KB-274" reference).
- **If `odoo_semantic_search` errors** (module not installed,
  provider down, etc.), fall back to `odoo_search_read` for this
  turn.

## Supported scope (v1)

Indexed: `knowledge.article` only.

Adding `ir.attachment`, `helpdesk.ticket`, `mail.message`, etc. is
a v1.5+ change: add a config row with the right `text_extractor`
and ship the extractor utility if not already present.

## Limits

- Brute-force cosine. Linear in corpus size. Comfortable up to
  ~100K vectors per tenant (~50 ms query). Past that, this module
  needs an ANN backend (FAISS / hnswlib in pure-Python wheels) —
  not in v1.
- Token budget per record: 8K tokens (text-embedding-3-small's
  context). Records that exceed are embedded on `name + first 8K
  chars` with a warning logged on the queue row.
- Daily token cap defaults to 1M (config). Cron pauses on overrun
  and resumes the next day.
- Provider HTTP timeouts: 30s connect, 60s read, 3 retries with
  exponential backoff inside the cron worker.

## Operations

### Initial install

1. Install module on the tenant's Odoo (standard apps menu).
2. Settings → Technical → AI Semantic Search.
3. Set provider kind, URL, API key, model, dimension. Save.
4. Click "Test provider" — should report `OK · 1536 dim · 80ms`.
5. Click "Reindex all" — kicks the cron immediately on every
   enabled model. For a fresh install with ~500 articles, expect
   a few minutes for the full sweep.

### Cost projection

```
articles × avg_tokens_per_article × $cost_per_1M_tokens / 1_000_000
```

For text-embedding-3-small at $0.02/1M:
- 1K articles × 1K tokens = $0.02 one-time.
- 5K articles × 2K tokens = $0.20 one-time.
- 10K articles × 5K tokens = $1.00 one-time.

Edits cost on the same scale per re-embed. Hash-skip eliminates
metadata-only writes from the cost equation.

### Logs

- Module emits to the standard Odoo logger under
  `odoo.addons.orc_client_semantic_search.*`.
- Cron worker logs one line per record processed (debug level)
  and one summary line per pass (info level): `processed=N
  errors=M skipped_hash=K`.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Search returns empty for an obvious query | Cron hasn't run since the article was created | Wait one cron interval, or click "Reindex all" |
| "Test provider" reports 401 | API key wrong or expired | Update key in Settings, save, retest |
| "Test provider" returns dim=N but config says M | Wrong `vector_dim` for the chosen model | Set vector_dim=N |
| Queue grows unbounded | Provider failing repeatedly; check `last_error` field on queue rows | Fix provider config or reset attempts |
| Cron paused with daily-cap message | Hit the daily token cap | Wait until midnight or raise cap |

## What's intentionally out of scope

- Image and table embeddings. Different vector space; cross-modal
  needs CLIP and a parallel index.
- Layout-aware PDF parsing (`unstructured`, `marker`). Pulls
  Tesseract / torch — Odoo.sh-hostile. Run extraction outside Odoo
  if a tenant ever needs it.
- Hybrid retrieval (BM25 + semantic with reciprocal-rank fusion).
  Postgres FTS is free; defer to v2 once we measure paraphrase
  recall isn't enough.
- Re-ranking with a cross-encoder. Latency cost without measured
  benefit at our scale.
- Chunking. Add when articles regularly exceed 8K tokens.
- A RAG framework dependency (LlamaIndex, LangChain). Not worth
  the transitive deps for a single HTTP call + numpy.
