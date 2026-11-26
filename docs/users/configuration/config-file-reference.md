# Engine and ingestion runtime configuration

Look up the `HARBORRAG_*` environment variables that the ingestion worker, the API,
chat and the MCP server read at startup, and the engine's two Python settings
objects. The variables are checked against `RuntimeSettings`
(`harborrag_runtime.config.settings`), the API's `ApiSettings`
(`harborrag_app.api.settings`) and the MCP server's launcher. Each variable is the
setting's name in capitals with the `HARBORRAG_` prefix, so `qdrant_url` is
`HARBORRAG_QDRANT_URL`.

Connectors, parsers, chunking, models and the graph build have their own YAML files;
this page lists only the variables that locate those files. Settings an administrator
changes at runtime are in [Runtime Settings](runtime-settings.md).
[Deployment](../../developers/deployment/README.md) says which `env/` file each
service reads in a repository checkout.

## Ingestion runtime environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_ENV` | `dev` | `dev`, `staging` or `prod`; anything but `dev` turns on the production checks, such as refusing a SQLite control database |
| `HARBORRAG_TENANT_ID` | `DEFAULT` | The one default tenant of the API (for a `*` credential that sends no `X-Tenant-Id`), the workers and the CLI, the `connectors.yaml` import and the settings seed; the removed `HARBORRAG_INGESTION_TENANT_ID` stops startup |
| `HARBORRAG_CONNECTOR_CONFIG_PATH` | `config/sources/connectors.yaml` | Connector file ([Connector Configuration](connector-config.md)) |
| `HARBORRAG_PARSER_CONFIG_PATH` | `config/ingestion/parsers.yaml` | Parser file ([Parser Configuration](parser-config.md)) |
| `HARBORRAG_CHUNKING_CONFIG_PATH` | `config/ingestion/chunking.yaml` | Chunking file; optional, built-in defaults apply when the default path is absent ([Chunking Configuration](chunking-config.md)) |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models/models.yaml` | Model catalog ([Model Configuration](model-config.md)) |
| `HARBORRAG_GRAPH_BUILD_CONFIG_PATH` | `config/graph/build.yaml` | Graph build file ([Graph Build Configuration](graph-build-config.md)) |
| `HARBORRAG_SETTINGS_SEED_PATH` | `config/settings.yaml` | Runtime settings seed file ([Runtime Settings](runtime-settings.md#seed-file-and-drift)) |
| `HARBORRAG_SCHEDULE_CONFIG_PATH` | `config/sources/schedules.yaml` | Declarative schedules the API syncs at startup; the API image sets `/app/config/sources/schedules.yaml` ([Scheduled Ingestion](../scheduled-ingestion.md)) |
| `HARBORRAG_CONTROL_DB_URL` | `sqlite+aiosqlite:///./harborrag_control.db` | Control database, where connections, runs and settings live; must not be SQLite unless `HARBORRAG_ENV=dev` |
| `HARBORRAG_CONTROL_DB_POOL_SIZE` | `40` | Connections each Postgres control-database pool keeps, 1 to 100 |
| `HARBORRAG_CONTROL_DB_MAX_OVERFLOW` | `32` | Extra connections each pool opens under load, 0 to 200 |
| `HARBORRAG_SECRETS_ENCRYPTION_KEY` | None | Encrypts stored connector credentials; with a control database that is not SQLite, every process that opens them (API, worker, checkout CLI) needs it, with the same value |
| `HARBORRAG_TEMPORAL_TARGET` | `localhost:7233` | Temporal frontend |
| `HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for Temporal without TLS that is not on loopback; ignored outside `dev`, like `connection.allow_insecure_remote` in `config/temporal.yaml` |
| `HARBORRAG_OBJECT_STORE_ENDPOINT_URL` | `http://localhost:9000` | S3-compatible object store |
| `HARBORRAG_OBJECT_STORE_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for plaintext object storage that is not on loopback; ignored outside `dev` |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` | Qdrant, the vector store, at its REST port |
| `HARBORRAG_QDRANT_PREFER_GRPC` | `true` | Send Qdrant calls over gRPC; `false` keeps them on the REST port of `HARBORRAG_QDRANT_URL` |
| `HARBORRAG_QDRANT_GRPC_PORT` | None | gRPC port of the same Qdrant, 1 to 65535; unset, gRPC goes to `6334` on the URL's host. Required while gRPC is preferred and the URL names a port other than `6333` |
| `HARBORRAG_QDRANT_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for plain-HTTP Qdrant that is not on loopback; ignored outside `dev` |
| `HARBORRAG_FALKORDB_HOST` | `localhost` | FalkorDB, the graph store |
| `HARBORRAG_FALKORDB_PORT` | `6379` | FalkorDB port |
| `HARBORRAG_FALKORDB_GRAPH` | `harborrag` | Graph name |
| `HARBORRAG_FALKORDB_MAX_CONNECTIONS` | `32` | FalkorDB connection pool size |
| `HARBORRAG_FALKORDB_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for FalkorDB without SSL that is not on loopback; ignored outside `dev` |
| `HARBORRAG_GRAPH_RELATION_REPAIR_CONCURRENCY` | `8` | Concurrent graph relation repairs |
| `HARBORRAG_RETIRED_VERSION_RETENTION_DAYS` | `30` | Days a retired document version keeps its object-store artifacts after projection cleanup; `0` purges at once, `off` keeps them |
| `HARBORRAG_RETIRED_VERSION_PURGE_INTERVAL_HOURS` | `24` | How often each ingestion worker sweeps every scope for expired retired versions; `0` disables the sweep, and the purge after each source run still applies |
| `HARBORRAG_SCHEDULE_METRICS_INTERVAL_SECONDS` | `30` | How often a worker exports schedule pause and overlap gauges; `0` disables it, and with several worker replicas enable it on one |
| `HARBORRAG_REDIS_URL` | None | Optional disposable cache and rate limiter |
| `HARBORRAG_REDIS_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for plaintext Redis that is not on loopback; ignored outside `dev` |
| `HARBORRAG_EMBEDDING_MODEL` | None | Logical model of the catalog's `embed` section, such as `primary`; set it only when the catalog is ambiguous or a non-default model is required |
| `HARBORRAG_EMBEDDING_DIMENSIONS` | None | Vector size, when the catalog does not settle it |

Database URLs, API keys, object-store credentials and FalkorDB passwords are secret.
`RuntimeSettings` masks them, and a failed settings check does not print the values it
refused. Supply them through the protected deployment environment, never through
checked-in files.

Outside `dev`, the `*_ALLOW_INSECURE_REMOTE` switches are ignored, so a remote Qdrant
needs HTTPS, a remote FalkorDB SSL, and a remote Temporal TLS.

The Qdrant client reads the REST port from `HARBORRAG_QDRANT_URL` but takes its gRPC
port from `HARBORRAG_QDRANT_GRPC_PORT`. When the URL names a port other than `6333`
and gRPC is preferred, the API, the worker, the MCP server and the checkout CLI refuse
to start until you set `HARBORRAG_QDRANT_GRPC_PORT` or
`HARBORRAG_QDRANT_PREFER_GRPC=false`. Without that check, a mapped port such as
`16333` would still send vectors to port `6334`, which can belong to another Qdrant.
The first time a process opens the vector store, it logs where vectors go, such as
`Vector store target=Qdrant gRPC localhost:16334`.

The pool size and overflow apply to each pool, and one process can open several pools
on the same database. Size Postgres `max_connections` for every pool of every API
replica, worker and checkout CLI. A worker refuses to start when the activities it runs
at once across its task queues exceed one pool's size plus overflow.

`HARBORRAG_EMBEDDING_MODEL` is not the endpoint's model name, which
`HARBORRAG_EMBED_MODEL` sets below.

## API process limits

Only the API reads these (`ApiSettings`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_API_CAPACITY_REDIS_URL` | None | Redis for rate and concurrency limits shared across API replicas; required unless `HARBORRAG_ENV=dev` |
| `HARBORRAG_API_REQUESTS_PER_MINUTE` | `60` | Requests per minute per authenticated principal |
| `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` | `4` | Concurrent requests per principal |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Time limit of one request handler |
| `HARBORRAG_OPS_PORT` | `9465` | Second port, which serves only Prometheus `/metrics`; must differ from `HARBORRAG_PORT` |
| `HARBORRAG_METRICS_SCRAPE_TOKEN` | None | Bearer token that `/metrics` requires. Unset, blank or only whitespace, `/metrics` answers `401` to every request, so give Prometheus a non-empty token |

## Model endpoint environment

HTTP and CLI chat, ingestion and retrieval share `HARBORRAG_MODEL_CONFIG_PATH`. The
checked-in `config/models/models.yaml` serves OpenAI and an OpenAI-compatible gateway
from one file, and references these protected values:

| Variable | Meaning |
| --- | --- |
| `HARBORRAG_CHAT_MODEL` | The endpoint's name for the `primary` chat model |
| `HARBORRAG_CHAT_API_KEY` | API key for chat |
| `HARBORRAG_EMBED_MODEL` | The endpoint's name for the `primary` embedding model |
| `HARBORRAG_EMBED_API_KEY` | API key for embedding; it may equal the chat key |
| `HARBORRAG_URL` | Optional. Base URL of another OpenAI-compatible endpoint for every family, such as a LiteLLM proxy gateway; the default is `https://api.openai.com/v1` |
| `HARBORRAG_EMBED_PROVIDER` | Optional. `cohere` when the embedding model is a Cohere model, so HarborRAG sends its `input_type`; the default is `openai_compatible`, and `openai` means the same |
| `HARBORRAG_RERANKER_MODEL` | Optional. The endpoint's name for a rerank model; set, it makes `retrieval.rerank` available |
| `HARBORRAG_RERANKER_API_KEY` | Optional while `HARBORRAG_RERANKER_MODEL` is unset, required once it is set. API key for reranking; a blank one is refused |

The first four are required, and a blank chat, embedding or rerank key is refused when
its client is built; see
[What the catalog refuses](model-config.md#what-the-catalog-refuses).
The model values are names the endpoint serves, as its `/models` path lists them.
Surrounding whitespace, such as a trailing carriage return, is trimmed. Changing
`HARBORRAG_EMBED_MODEL` or `HARBORRAG_EMBED_PROVIDER` needs a re-index; see
[The shipped catalog](model-config.md#the-shipped-catalog).

Keep the populated values in the git-ignored `env/.env.models` file or a secret
manager. The catalog loader does not read `.env` files: in a checkout the CLI loads
`env/.env.models` itself, and Compose passes it to the API, worker and MCP
containers.

## MCP runtime environment

The MCP server keeps transport and tool policy separate from model policy:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_MCP_CONFIG_PATH` | `config/mcp/server.yaml` | Versioned tool defaults, limits, enablement and tenant overrides |
| `HARBORRAG_MCP_AUDIT_PATH` | `.harborrag/mcp-audit.jsonl` | Owner-only audit output of the `harborrag-mcp` launch; a relative path is under the home directory. The launch creates a missing directory with mode `0700`, and refuses to start when the directory grants access to other users or belongs to another user. A server built in Python passes `audit=McpAuditLog(path=...)` |
| `HARBORRAG_MCP_BEARER_TOKEN` | None | Local HTTP owner token; at least 32 bytes |
| `HARBORRAG_MCP_HOST` | `127.0.0.1` | HTTP bind address. One that is not loopback is refused outside a development container; see `HARBORRAG_MCP_ALLOW_INSECURE_BIND` |
| `HARBORRAG_MCP_ALLOWED_HOSTS` | None | Comma-separated host names the HTTP server answers besides `127.0.0.1`, `localhost` and `::1`, such as a reverse proxy's |
| `HARBORRAG_MCP_ALLOW_INSECURE_BIND` | `false` | `true` accepts a bind beyond loopback, where tokens travel over plain HTTP. Honoured only with `HARBORRAG_ENV=dev` and with `HARBORRAG_MCP_ALLOWED_HOSTS` set. The Compose launcher sets it for its container, whose port Docker publishes on loopback |
| `HARBORRAG_MCP_PORT` | `8010` | Local HTTP and status UI port |
| `HARBORRAG_MCP_PATH` | `/mcp` | Streamable HTTP MCP path |
| `HARBORRAG_MCP_RATE_LIMIT` | `10` | Requests per second each credential may send to `/mcp` and `/api/tools` together, which is also its burst; `0` turns the limit off |
| `HARBORRAG_MCP_MAX_INFLIGHT` | `4` | Requests one credential may have running at once on those two paths; `0` turns the limit off |

A request over either limit is refused at once: `/mcp` answers with JSON-RPC error
`-32000`, and `/api/tools` with `429` and a `Retry-After` header. Each server process
keeps its own budgets, so the reader server and the Explorer server count separately.
stdio, `/healthz` and the status page are not limited. A limit that is not `0` or a
positive integer stops the launcher.

`scripts/deployment/dev.sh bootstrap` creates `env/.env.mcp` with a generated bearer
token. Reader keys, the status UI and the tool settings are covered in
[MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).

## Engine settings objects

The engine itself takes two small frozen dataclasses, built in Python.

### `EngineConfig`

```python
from harborrag_engine.config import EngineConfig

config = EngineConfig(tenant="default", environment="local")
```

| Field | Default | Meaning |
| --- | --- | --- |
| `tenant` | `default` | Tenant label included in engine diagnostics |
| `environment` | `local` | Free-form environment label included in diagnostics |

### `EnginePolicy`

```python
from harborrag_engine.policy import EnginePolicy

policy = EnginePolicy(max_concurrency=4, retrieval_top_k=10)
```

| Field | Default | Meaning |
| --- | --- | --- |
| `max_concurrency` | `4` | Engine concurrency policy; must be at least one |
| `retrieval_top_k` | `10` | Default retrieval result count |

`CompositionRoot.diagnostics()` reports the configured engine environment, tenant and
concurrency. `CompositionRoot.production()` assembles those engine settings with the
control-plane repositories.
