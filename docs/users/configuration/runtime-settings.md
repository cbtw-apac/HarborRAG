# Runtime settings

Look up the six settings an administrator can change while HarborRAG runs, without
editing a file or restarting a service: the levels they apply at, when a change takes
effect, and how to change them over HTTP, from a file, or from the command line
([Import and export from the CLI](#import-and-export-from-the-cli)).

A change is stored as an override in the control database, the database where
HarborRAG keeps its own state such as connections, runs and settings. A key without an
override keeps its boot setting, the value the process read from its files and
environment at startup. Everything else (connectors, models, Temporal, stores,
authentication) stays in the files and environment variables described in
[Configuration](README.md).

## Settings and their levels

| Key | Type | Levels | Applies | Default |
| --- | --- | --- | --- | --- |
| `retrieval.top_k` | integer 1-100 | tenant, project | `live` | `5` |
| `retrieval.rerank` | boolean | tenant, project | `live` | `false` |
| `parsing.pdf_parser` | `liteparse` or `docling` | tenant, project, connection | `next_ingestion` | `liteparse` |
| `chunking.max_tokens` | integer 64-4096 | tenant, project, connection | `next_ingestion` | `1200` |
| `ingestion.concurrency` | integer 1-64 | tenant, connection | `next_ingestion` | `8` |
| `models.embedding` | `provider:model` | tenant, project | `reindex` | the first deployment of `embed.default_model` in `config/models/models.yaml` |

The **Applies** column says when a change takes effect:

- `live`: running processes pick the change up within 10 seconds.
- `next_ingestion`: the next ingestion run uses it; a run already started keeps its
  values.
- `reindex`: changing it needs a re-index, which a settings change cannot start.
  `PATCH` answers `409 reindex_required`, and the value is never stored in this
  release.

What each key does:

- `retrieval.top_k` caps the chat retrieval and the MCP `vector_search` default. MCP
  calls carry no project, so MCP reads the tenant level only; a project override
  applies to chat sessions in that project. The MCP limits `limits.top_k` and
  `policy.max_results` still clamp the value. On a SQLite control database the MCP
  server keeps the `config/mcp/server.yaml` default.
- `retrieval.rerank: true` needs a `rerank` section in the model catalog; without one
  it is refused with `422`. See [Rerank chat retrieval](#rerank-chat-retrieval).
- `parsing.pdf_parser` picks the PDF engine; `docling` needs extra setup, see
  [What reprocesses on the next sync](#what-reprocesses-on-the-next-sync).
- `chunking.max_tokens` caps `maximum_tokens` of every profile in
  `config/ingestion/chunking.yaml`; a profile already at or below the cap is unchanged.
  Route chunks keep their 512-token cap.
- `ingestion.concurrency` is the number of documents processed at once. An explicit
  value on the submission (`harborrag ingest start --document-concurrency`) wins over
  it.
- Keys that start with `database.`, `redis.`, `temporal.`, `qdrant.`, `graph.`,
  `listeners.`, `auth.`, `telemetry.` or `secrets.` are boot-only and are refused at
  every entry point.

`GET /v1/admin/settings/schema` returns the same table as JSON, grouped as
`{"groups": [{"name", "title", "settings"}]}` in the order `retrieval`, `parsing`,
`chunking`, `ingestion`, `models`. Each setting has `key`, `type` (`integer`,
`boolean`, `enum` or `model_ref`), `default`, `levels`, `applies` and `description`.
A bounded key also has `min` and `max`, and an enumerated key has `enum`; a key
without them leaves those fields out.

## Levels and precedence

A level is `tenant`, `project:<project_id>` or
`connection:<project_id>/<connection_id>`. A request without a level means `tenant`. The
effective value resolves default, then tenant, then project, then connection; the most
specific override wins. An override is accepted only at the levels its key lists.

A key with no override leaves its boot setting in charge. The registry default equals
the shipped value of that boot setting, so upgrading changes nothing. An override at
any level replaces the boot setting.

| Key | Boot setting that stays in charge until overridden |
| --- | --- |
| `retrieval.top_k` | `tools.vector_search.defaults.top_k` in `config/mcp/server.yaml` (MCP); `HARBORRAG_CHAT_RETRIEVAL_TOP_K` (chat) |
| `parsing.pdf_parser` | the enabled `parser: pdf` definition of `config/ingestion/parsers.yaml` |
| `chunking.max_tokens` | the `maximum_tokens` of each profile in `config/ingestion/chunking.yaml` |
| `ingestion.concurrency` | `ingestion.document_concurrency` in `config/temporal.yaml` or `HARBORRAG_TEMPORAL_INGESTION_DOCUMENT_CONCURRENCY` |

`retrieval.rerank` has no boot setting: chat does not rerank until the key is
overridden.

Because a key at its default reports the registry default, `GET` shows `5` for
`retrieval.top_k` even if you changed `HARBORRAG_CHAT_RETRIEVAL_TOP_K` to `8`. To
carry such a change over, see [Upgrading](../upgrading.md).

## Rerank chat retrieval

`retrieval.rerank: true` makes chat reorder its passages with the rerank model of
`config/models/models.yaml`
([The shipped gateway catalog](model-config.md#the-shipped-gateway-catalog)).

- Only chat reads it, over HTTP and with `harborrag chat`, at the same level as
  `retrieval.top_k`: the session's project, or the one project a one-off question
  names, else the tenant. The MCP `vector_search` tool and `harborrag retrieve` do not
  rerank.
- Retrieval fetches a pool of four times `top_k` passages, at most 100 and never fewer
  than `top_k`. The rerank model orders the pool, and chat keeps the first `top_k`.
- Each reranked result carries the model's score in its `metadata` as `rerank_score`;
  chat citations do not carry it. Its `relevance` stays the lane's similarity, which
  `HARBORRAG_CHAT_RETRIEVAL_MIN_RELEVANCE` and the MCP `score_threshold` compare
  against.
- A rerank provider error fails the retrieval, the same as an embedding error.
- `PATCH` and `harborrag config import` accept `true` only when the catalog has a
  `rerank` section; otherwise they answer `422 validation_error` with
  `field = "retrieval.rerank"`.
- The API reads the catalog's `rerank` section when it starts, so restart it after you
  add or remove the section. An override still on after the section is gone is served
  in retrieval order, and each such turn logs a warning.

## What reprocesses on the next sync

A document's processing identity is the set of inputs that decide how it is parsed and
chunked. Parsing and chunking settings are part of it. After the first override of any
`parsing.*` or `chunking.*` key, the next sync of each affected connection reprocesses
all of its documents. Removing the override reprocesses them once more.
`ingestion.concurrency` and the `retrieval.*` keys do not change the identity.

`parsing.pdf_parser: docling` also needs a `parser: pdf` definition with a single
`docling` engine in `config/ingestion/parsers.yaml`, added by the operator with
`enabled: false`, and the docling extra installed in the worker image. Without them,
documents fail with `document_release_parser_unavailable`, recorded as retryable: add
the definition, then retry the run with
`POST /v1/admin/ingestions/{ingestion_id}/retry-failures`; the retry replays the parser
the run was pinned to. HarborRAG never edits the shipped `parsers.yaml` and
`chunking.yaml`, because their bytes are part of the identity too.

Editing `config/ingestion/parsers.yaml`, for example to add the docling definition,
therefore changes the identity of every connection: the next sync of each one
reprocesses all of its documents, whether or not it has a `parsing.*` override. Put
the same file in the API and in the worker before you set the override; the dev
Compose files mount the same `config/` into both, so restart them. With different
files, runs started from the API and scheduled runs prepared by the worker carry
different identities, and documents reprocess on alternate syncs. Documents published
by a retry run keep the identity of the run it retries, so the first sync after the
edit reprocesses them once more. See
[Use docling for PDFs](../upgrading.md#use-docling-for-pdfs).

## Run pinning

The process that prepares an ingestion resolves the settings once and stores them with
the run. The ingestion shows the `settings_revision` it used. Pausing, recovery and
retries keep the root run's values; a change made during a run affects only the next
one.

## Read and change settings over HTTP

These calls need an API bearer token in `$TOKEN`; see
[Configure API authentication](workspace-mode.md#configure-api-authentication). The
dev stack's `env/.env.api` sets `HARBORRAG_AUTH_MODE=none`, a development-only mode
that treats every request as an owner.

`GET /v1/admin/settings?level=<level>` returns the overrides stored at the level; add
`effective=true` for every key's `{value, origin}`. The `ETag` header carries the
revision, for example `W/"rev-3"`; the body carries `revision: "rev_3"` (an empty log
is `rev_0`).

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: DEFAULT" \
  "http://127.0.0.1:8000/v1/admin/settings?effective=true"
```

`PATCH /v1/admin/settings?level=<level>` takes a JSON object. `If-Match` is required:
send the `ETag` from your last read, or `*` for the current revision.

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: DEFAULT" \
  -H "If-Match: *" -H "Content-Type: application/json" \
  -d '{"retrieval.top_k": 3}' \
  "http://127.0.0.1:8000/v1/admin/settings?level=tenant"
```

The response lists `changed` items `{key, from, to, applies}`: `from` is the effective
value at the level before the change and `to` the new override. A key whose override
does not change is left out, and a write that changes nothing adds no revision. A
`null` value removes the override. `dry_run=true` saves nothing and answers with the
current revision.

| Status | Cause |
| --- | --- |
| `403 forbidden` | the role is too low (below) |
| `409 reindex_required` | the key applies as `reindex` |
| `412 precondition_failed` | `If-Match` is stale, so someone else changed settings first |
| `422 validation_error` | no `If-Match` (`field = "If-Match"`); unknown key; wrong type or range; key not allowed at the level; boot-only key; `retrieval.rerank: true` while the model catalog has no `rerank` section; `?reindex=true`; malformed `level` |
| `422 reference_not_found` | the project or connection in `level` does not exist (`field = "level"`) |

Nothing is saved when any key is refused.

| Call | Lowest role |
| --- | --- |
| `GET /v1/admin/settings` | `admin` for any level; `editor` for `connection:` levels only |
| `PATCH /v1/admin/settings` | `admin` |
| `GET /v1/admin/settings/schema` | `editor` |

## Seed file and drift

`config/settings.yaml` seeds the tenant named by `HARBORRAG_TENANT_ID` once, at the
first API start when that tenant has no revisions. The path is
`HARBORRAG_SETTINGS_SEED_PATH` (`/app/config/settings.yaml` in the API image). The file
is git-ignored. `config/settings.example.yaml` lists every tenant-level key you can set
at its default; do not use it whole, because each key in the seed file becomes an
override that replaces its boot setting. Keep only the keys you mean to override.

Later starts do not re-apply the file. When it differs from the stored values, the API
logs one warning, `config_yaml_drift path=<path> keys=<k1,k2,...>`, and the stored
values stay. Change settings through the API or `harborrag config import`.

The seed file, `import` and `export` share one shape. Keys may be dotted or nested.
Every top-level entry other than `version`, `projects` and `connections` is tenant
level; `version`, when present, is `1`.

```yaml
version: 1
retrieval.top_k: 5
ingestion.concurrency: 8
projects:
  demo:
    retrieval.top_k: 8
connections:
  "demo/docs-folder":
    chunking.max_tokens: 512
    parsing.pdf_parser: liteparse
```

## Import and export from the CLI

```bash
harborrag config import settings.yaml
harborrag config import settings.yaml --apply
harborrag config export --level tenant --output settings.yaml
```

`import` is a dry run unless `--apply` is given; `--apply` writes the file as one
revision. `--prune` also removes every stored override the file lacks, at any level. An
invalid file exits `1`, names the file and the key, and applies nothing; boot-only keys
are reported and skipped. `export` writes registry keys only, in the shape above, and
`import` reads the result back unchanged. Overrides of a deleted project or connection
stay in the store but are left out of the export, each named on stderr. Both take
`--tenant` (default `DEFAULT`, or `HARBORRAG_TENANT_ID`); `export` takes `--level`
(default: every level) and `import` takes `--json`. Imports are recorded in the revision
log under the operator's `user@host`.

From a repository checkout, `config import --apply` and `config export --output`
refuse the local SQLite control database until the CLI points at the stack's Postgres
with `HARBORRAG_CONTROL_DB_URL`; see
[Checkout quick start](../../developers/checkout-quick-start.md).

## Propagation and outages

A write notifies every process through Postgres `LISTEN`/`NOTIFY`, and each process
also polls the revision every 5 seconds, which covers SQLite and a missed
notification. A change reaches every process within 10 seconds.

Each process caches settings. The first load of a tenant must succeed; if Postgres is
down then, the call fails with an unavailable error instead of serving defaults. If a
later reload fails, the process keeps serving the last snapshot and logs
`settings_cache_stale tenant_id=<id>`. `/readyz` reports `postgres: fail` while the
database is down, and the cache reloads after it returns.
