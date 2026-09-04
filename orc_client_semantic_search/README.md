# `orc_client_semantic_search`

Permission-aware semantic search over Odoo records, exposed to the
AI Workplace agent via a single XML-RPC method.

The agent today finds Odoo records with hand-built keyword domains
(`name ilike "..."`). That breaks on paraphrase: a user asking
"how do we bill prepayments?" never reaches an article titled
"Down-payment workflow". This module fixes the recall problem by
indexing record text with vector embeddings, while leaving
permissions where they already work — Odoo's `ir.rule` filters every
read by the agent's principal user.

## How it fits

- **This module** holds embeddings + runs the search inside the
  tenant's Odoo. It calls the embedding provider directly; the AI Workplace
  gateway is not in the embed path.
- **`odoo-mcp`** (in the AI Workplace repo) gains one new typed tool,
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

**Bookkeeping runs with elevated rights.** The `orc.embedding*`
models are internal plumbing, not business data: an end user never
addresses them directly, they only get touched as a side effect of
a business action (writing an article, running a search). So every
internal access to them — the create/write/unlink hooks that
maintain the queue, and the config reads that build the provider —
runs `sudo()`. The alternative, granting write access on the queue
to every internal user, would put a technical model in the reach
of the UI and still not cover portal-side writes. The business
record itself is **never** sudo'd: the article recordset stays on
the user's own environment, so `ir.rule` decides as before.

**Business-user explainer:** see
[`docs/semantic-search-security.md`](docs/semantic-search-security.md)
for a sequence diagram + narrative aimed at admins / operators who
need to understand the permission model without reading the engine
schema.

## Index scope — what gets sent to the provider

The permission model above governs who may *read* a result. It says
nothing about which records leave the tenant in the first place, and
those are different questions: embedding a record transmits its text
to a third-party API, which happens once, at index time, with no end
user present and no `ir.rule` in the path.

Scope is therefore decided by the operator, on the per-model config
row, along three axes. All three are evaluated by one predicate
(`orc.embedding.config._filter_indexable`), so the enqueue hooks,
the cron, "Reindex all" and the preview cannot disagree about what
is in scope.

1. **`enabled`** — the model-wide switch. Off means nothing from
   this model is indexed or searched.
2. **`index_domain`** — an Odoo domain string, evaluated against the
   indexed model. Empty means every record. Two `knowledge.article`
   examples, both on stored searchable fields:
   `[("category", "!=", "private")]` keeps personal articles out, and
   `[("is_article_visible_by_everyone", "=", True)]` narrows to what
   the whole company can already see. A domain that doesn't parse, or
   that the model rejects, fails at save — not silently in the cron.
3. **`orc_ai_index_exclude`** — a per-record boolean, set by whoever
   can edit the record. True means never index this one, whatever
   the domain says. On `knowledge.article` it is an optional column
   in the article **list** view plus an "Excluded from AI index"
   search filter. Not on the article form: that view is a custom
   OWL layout with no stable anchor for a checkbox, and a bad xpath
   into an Enterprise view is an install failure rather than a
   cosmetic problem. Placing it on the form is a follow-up for
   someone who can render the view.

Scope is **retroactive, and narrowing is asymmetric with widening.**

A record that falls out of scope loses its `orc.embedding` row and
its queue marker. Which happens when depends on what changed:

| What changed | When the vector goes |
|---|---|
| `index_domain` or `enabled` on the config row | **On save.** The cron only walks queue rows, and a record that quietly fell out of scope has none — so waiting for the cron would mean waiting forever |
| The per-model config row is **deleted** | Immediately. No row is an empty scope, and orphaned vectors would come back the moment an equivalent row was re-created |
| `orc_ai_index_exclude` on a record | Next cron pass (the write enqueues it) |
| A field the domain reads — e.g. `category` under `[("category", "!=", "private")]` | Next cron pass (same mechanism; the watched set is derived from the domain) |

Deleting the **global** row purges nothing — it holds provider
credentials, not a scope.

**Widening does not auto-apply.** Saving a broader domain purges
nothing (there is nothing to purge) and enqueues nothing, because
enqueueing sends records to the provider and that is a spend. Use
"Sync index scope" or "Reindex all" — both confirmed — to fill the
gap. Narrowing is free and safety-relevant, so it is automatic;
widening costs money, so it stays explicit.

One residual gap, by construction: a domain over a **related**
record's field (`parent_id.category`) re-evaluates when the article
is re-parented, but not when that parent's own category changes —
nothing fires a hook on the article. "Sync index scope" is the
answer for those.

**A domain that stops evaluating is not an exclusion.** The domain is
validated when saved, but a later module upgrade can rename or drop
the field it names. When that happens the module distinguishes "out
of scope" from "scope unknown" and, for the second, does *nothing*:
no embedding (we can't say a record is in scope), no purge (we can't
say it isn't), the queue untouched, an ERROR per cron pass naming the
model and the reason, and preview / sync / **Reindex all** all
refusing with the same message. Reindex all matters most here: it
deletes the index before rebuilding it, so a silent fail-closed would
destroy the corpus and re-enqueue nothing. Article saves keep working
— the predicate runs inside the author's transaction, so it swallows
the failure rather than rolling back their edit. Fix the domain and
the next pass proceeds normally.

`enabled = False` is the one case the guard does **not** cover, and
deliberately: a disabled row's scope isn't unknown, it's empty, so
disabling purges whatever the domain's state. Otherwise a broken
domain could suppress that purge and re-enabling the row would put
the stale vectors straight back into `semantic_search`, which gates
on `enabled` rather than on scope.

Deleting the row removes the vector and the tenant's ability to
search it. It does **not** un-send the text: the provider received
it at index time. Scope controls what will be transmitted, not what
already was. Narrowing scope on a live index is damage limitation,
not erasure — for erasure you need the provider's own retention
terms.

**Preview before you widen.** "Preview scope" on a per-model row
counts what the current settings would do — including *how many
records would be sent to the provider if the cron ran now* — and
calls nothing. Use it before setting the API key on a fresh install,
and before every domain edit.

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
| `provider_api_key` | char | Stored **as plain text**; the form masks it and the ACL limits reads to the technical group. Not encrypted at rest (only on global row) |
| `provider_model` | char | e.g. `text-embedding-3-small` (only on global row) |
| `vector_dim` | int | e.g. 1536. Must match `provider_model` (only on global row) |
| `cron_interval_minutes` | int | Default 5. **Descriptive only** — the actual cadence is the `ir.cron` record's own interval; editing this field does not move the cron (only on global row) |
| `daily_token_cap` | int | Tokens the cron may spend per day. `0` pauses the cron. Counted from the provider's reported usage, falling back to a chars÷4 estimate (only on global row) |
| `tokens_used_today` | int | Running total for `tokens_usage_date`; reset by the cron on the first pass of a new day (only on global row) |
| `tokens_usage_date` | date | The day `tokens_used_today` refers to (only on global row) |
| `model_name` | char | The Odoo model to index (only on per-model rows) |
| `enabled` | bool | Whether to index this model (only on per-model rows). Setting it False purges that model's vectors on save |
| `index_domain` | char | Odoo domain string limiting which records of `model_name` are in scope. Empty = all. Validated at save; narrowing it purges on save (only on per-model rows) |
| `text_field_path` | char | Dotted path to the text source. `body` for `knowledge.article`. Future models may use `description` or `name + body` (only on per-model rows) |
| `text_extractor` | selection | `html_strip` (default for HTML fields), `plain` (no transform), `attachment` (run pypdf etc.) — only on per-model rows |

Singleton enforcement: a unique constraint on `is_global=True` (only
one global row may exist). Per-model rows must have
`is_global=False` and a unique `model_name`.

### `orc_ai_index_exclude` (one boolean per indexable record)

| Field | Type | Notes |
|---|---|---|
| `orc_ai_index_exclude` | bool | On the indexed model itself, not on `orc.embedding`. True = never index this record. Indexed in SQL, since the predicate filters on it |

Added to `knowledge.article` by this module. A model with no such
field is treated as "nothing excluded" — the predicate checks
`_fields` rather than requiring every future model to carry it, so
adding a model stays a config-row change.

Writable by anyone who can edit the record: the person who knows a
given article is sensitive is its author, not the sysadmin. Setting
it enqueues the record, and the cron then deletes any vector it
already had.

### `orc.embedding.queue` (pending reindex markers)

| Field | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `model` | char | Same as `orc.embedding.model` |
| `res_id` | int | |
| `enqueued_at` | datetime | Set when the marker is created |
| `attempts` | int | Incremented on each failed cron pass. **Not a ceiling** — the cron retries a failing row on every subsequent pass, indefinitely, including on errors that will never succeed (a 401). Read it as an age-of-failure counter, and watch `last_error` |
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
- **Vector dim** — int. Must match the chosen model. Checked by the
  **"Test provider"** button, which embeds the string `"ping"` and
  compares dimensionality. Not checked on save — a wrong value sits
  there until you press the button or the cron starts failing.
- **Cron interval (minutes)** — int. Default 5. Descriptive; change
  the cadence on the `ir.cron` record under Settings → Technical →
  Scheduled Actions.
- **Daily token cap** — int. Default 1,000,000. The cron adds up the
  tokens it spends per calendar day and stops for the rest of the day
  on overrun, resuming at midnight. **`0` pauses the cron entirely**
  — that is the documented way to stop indexing without uninstalling,
  so a cleared field stops the sweep rather than uncapping it.

### 2. Indexed models (per-model rows)

A list view + form. v1 ships one row pre-configured:

| model_name | enabled | index_domain | text_field_path | text_extractor |
|---|---|---|---|---|
| `knowledge.article` | True | *(empty — all articles)* | `body` | `html_strip` |

Adding a model is data-driven: create a new row, set
`text_field_path`, save. The cron picks it up on the next pass.

Two buttons on the per-model form, both about scope:

- **"Preview scope"** — counts only, calls nothing, writes nothing.
  Reports total records, how many each axis excludes, how many are
  already indexed, how many vectors would be **deleted** as now
  out-of-scope, and how many records would be **sent to the provider**
  if the cron ran now. That last number is the one to read before
  widening a domain or filling in the API key. It counts in-scope
  records with no vector **plus** in-scope records carrying a pending
  edit marker — an edited article keeps its vector and is re-sent —
  and it rounds up: a queued record whose text turns out unchanged is
  hash-skipped and costs nothing.
- **"Sync index scope"** — applies the current scope immediately
  rather than waiting for the cron: deletes out-of-scope vectors and
  queue markers, enqueues in-scope records that have no vector yet.
  Idempotent; a second press on an unchanged corpus does nothing. It
  enqueues, so the embedding cost lands on the cron's next pass, not
  on the button.

### 3. Index status

There is no status dashboard. Counts are read from the two models
directly (Settings → Technical, or the developer-mode list views on
`orc.embedding` and `orc.embedding.queue`); "Preview scope" above is
what reports them per model.

Two buttons on the global form:

- **"Reindex all"** button — drops the index for every enabled model
  and enqueues every **in-scope** record. Respects `index_domain` and
  `orc_ai_index_exclude`, so it can no longer be the way a filtered
  corpus gets sent in full. Confirmation modal, because of the cost.
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
   write hook → in scope? ──no──> nothing enqueued
            │ yes
            ▼
   upsert orc.embedding.queue row
            │
   ┌────────▼────────┐
   │   ir.cron job   │  every N min (ir.cron's own interval)
   └────────┬────────┘
            │  daily token cap spent, or cap == 0? → stop this pass
            │
            │  for each queue row:
            │    1. read source record
            │    2. STILL in scope?  ──no──> delete orc.embedding row,
            │                                drop queue row, next
            │    3. extract text per text_extractor
            │    4. hash; if matches existing orc.embedding.content_hash → drop queue row, no-op
            │    5. else: call provider.embed(text); add its tokens to today's total
            │    6. upsert orc.embedding row, drop queue row
            │  on provider error:
            │    bump attempts, store last_error, leave queue row in place
            │    (retried on every later pass — no ceiling)
            │
            ▼
[orc.embedding row updated, queue row gone]
```

Step 2 is the authoritative gate for anything that *reaches* the
sweep: a queue row can outlive the settings that created it, so
tightening `index_domain` while rows are pending drops those rows
rather than embedding them.

What step 2 cannot see is a record with no queue row. That is why
saving a narrowed `index_domain` (or `enabled = False`) purges
directly instead of relying on the sweep — see the table under
"Index scope". The two together are what make the claim
"out-of-scope records hold no vector" true rather than
eventually-true-if-someone-edits-them.

Failure accounting worth knowing: an embed call that reaches the
provider, gets billed, and *then* fails our own dimension check is
charged against the daily cap anyway. Without that, a wrong
`vector_dim` would bill on every pass indefinitely — there is no
attempt ceiling — while the counter stayed at zero. A call that
never reached the provider (network error) is charged nothing.

For the same reason the counter is written on **its own cursor**,
committed independently of the sweep. A provider charge can't be
rolled back, so neither can the record of it: if a later row in the
pass fails and Odoo unwinds the transaction, the accounting has to
survive, or the next pass re-sends the same records with the cap
still reading zero.

When a record is **deleted**, the corresponding `orc.embedding`
row is removed via an `unlink` hook (cascade by `(model, res_id)`).
When a record is **archived**, the embedding stays (the agent will
never see it because reads filter on `active=True` by default) — put
`("active", "=", True)` in `index_domain` if you want archiving to
drop the vector too.

Flipping `orc_ai_index_exclude` is a `write`, so it enqueues like any
other indexed change; the cron then takes the step-2 branch and
deletes the vector. Clearing it enqueues again and the record is
re-embedded — at the cost of one more provider call.

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
- Daily token cap defaults to 1M (config). The cron stops for the
  rest of the calendar day on overrun and resumes at midnight; `0`
  pauses it outright.
- Provider HTTP timeouts: as configured on the provider
  (`timeout_connect` / `timeout_read`). **No backoff and no retry
  ceiling** — a failing row is retried on every subsequent cron pass,
  forever, whatever the status code. A wrong API key therefore
  produces one failed call per queued record per pass until someone
  fixes it. Bounded retry is a follow-up, not shipped.

## Operations

### Initial install

1. Install module on the tenant's Odoo (standard apps menu).
2. Settings → Technical → AI Semantic Search.
3. **Decide scope before you decide provider.** On the
   `knowledge.article` row, set `index_domain` (or leave it empty
   deliberately), then click **"Preview scope"** and read the
   "would be sent to the provider" figure. Installing the module
   leaves `enabled=True` with an empty domain, so the only thing
   standing between a fresh install and the whole corpus is the
   unset API key — filling it in at step 4 is the act that starts
   transmission.
4. Set provider kind, URL, API key, model, dimension. Save.
5. Click "Test provider" — should report `OK · 1536 dim · 80ms`.
6. Click "Reindex all" (or "Sync index scope" if you only want the
   in-scope gap filled). For a fresh install with ~500 in-scope
   articles, expect a few minutes for the full sweep.

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
| Search returns empty for an obvious query | Cron hasn't run since the article was created — or the article is out of scope | "Preview scope" on the model row; if it's excluded that's the answer. Otherwise wait one cron interval |
| One article never appears, others do | `orc_ai_index_exclude` is set on it, or `index_domain` excludes it | Both are visible on the record / config row |
| "Test provider" reports 401 | API key wrong or expired | Update key in Settings, save, retest |
| "Test provider" returns dim=N but config says M | Wrong `vector_dim` for the chosen model | Set vector_dim=N |
| Queue grows unbounded | Provider failing repeatedly; check `last_error` field on queue rows | Fix provider config. There is no attempt ceiling, so the rows keep retrying until the cause is fixed or they're deleted |
| Cron logs `daily token cap reached` and stops | Hit `daily_token_cap` for today | Wait until midnight or raise the cap. `tokens_used_today` on the global row shows the running total |
| Indexing stopped and the log says `paused` | `daily_token_cap` is 0 | That's the documented pause switch — set a positive cap to resume |

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
