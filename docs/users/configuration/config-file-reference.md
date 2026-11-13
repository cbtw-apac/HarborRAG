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
| `HARBORRAG_INGESTION_TENANT_ID` | `DEFAULT` | Default tenant of the `ingest`, `source` and `config` commands, the `connectors.yaml` import and the settings seed |
| `HARBORRAG_CONNECTOR_CONFIG_PATH` | `config/sources/connectors.yaml` | Connector file ([Connector Configuration](connector-config.md)) |
| `HARBORRAG_PARSER_CONFIG_PATH` | `config/ingestion/parsers.yaml` | Parser file ([Parser Configuration](parser-config.md)) |
| `HARBORRAG_CHUNKING_CONFIG_PATH` | `config/ingestion/chunking.yaml` | Chunking file; optional, built-in defaults apply when the default path is absent ([Chunking Configuration](chunking-config.md)) |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models/models.yaml` | Model catalog ([Model Configuration](model-config.md)) |
| `HARBORRAG_GRAPH_BUILD_CONFIG_PATH` | `config/graph/build.yaml` | Graph build file ([Graph Build Configuration](graph-build-config.md)) |
| `HARBORRAG_SETTINGS_SEED_PATH` | `config/settings.yaml` | Runtime settings seed file ([Runtime Settings](runtime-settings.md#seed-file-and-drift)) |
| `HARBORRAG_SCHEDULE_CONFIG_PATH` | `config/sources/schedules.yaml` | Declarative schedules the API syncs at startup; the API image sets `/app/config/sources/schedules.yaml` ([Scheduled Ingestion](../scheduled-ingestion.md)) |
| `HARBORRAG_CONTROL_DB_URL` | `sqlite+aiosqlite:///./harborrag_control.db` | Control database, where connections, runs and settings live; must not be SQLite unless `HARBORRAG_ENV=dev` |
| `HARBORRAG_SECRETS_ENCRYPTION_KEY` | None | Encrypts stored connector credentials; with a control database that is not SQLite, every process that opens them (API, worker, checkout CLI) needs it, with the same value |
| `HARBORRAG_OBJECT_STORE_ENDPOINT_URL` | `http://localhost:9000` | S3-compatible object store |
| `HARBORRAG_OBJECT_STORE_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for plaintext object storage that is not on loopback; ignored outside `dev` |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` | Qdrant, the vector store |
| `HARBORRAG_FALKORDB_HOST` | `localhost` | FalkorDB, the graph store |
| `HARBORRAG_FALKORDB_PORT` | `6379` | FalkorDB port |
| `HARBORRAG_FALKORDB_GRAPH` | `harborrag` | Graph name |
| `HARBORRAG_FALKORDB_MAX_CONNECTIONS` | `32` | FalkorDB connection pool size |
| `HARBORRAG_GRAPH_RELATION_REPAIR_CONCURRENCY` | `8` | Concurrent graph relation repairs |
| `HARBORRAG_RETIRED_VERSION_RETENTION_DAYS` | `30` | Days a retired document version keeps its object-store artifacts after projection cleanup; `0` purges at once, `off` keeps them |
| `HARBORRAG_RETIRED_VERSION_PURGE_INTERVAL_HOURS` | `24` | How often each ingestion worker sweeps every scope for expired retired versions; `0` disables the sweep, and the purge after each source run still applies |
| `HARBORRAG_SCHEDULE_METRICS_INTERVAL_SECONDS` | `30` | How often a worker exports schedule pause and overlap gauges; `0` disables it, and with several worker replicas enable it on one |
| `HARBORRAG_REDIS_URL` | None | Optional disposable cache and rate limiter |
| `HARBORRAG_REDIS_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for plaintext Redis that is not on loopback; ignored outside `dev` |
| `HARBORRAG_EMBEDDING_MODEL` | None | Logical model of the catalog's `embed` section, such as `primary`; set it only when the catalog is ambiguous or a non-default model is required |
| `HARBORRAG_EMBEDDING_DIMENSIONS` | None | Vector size, when the catalog does not settle it |

Database URLs, API keys, object-store credentials and FalkorDB passwords are secret.
`RuntimeSettings` masks them; supply them through the protected deployment
environment, never through checked-in files.

`HARBORRAG_EMBEDDING_MODEL` is not the gateway's model name, which
`HARBORRAG_EMBED_MODEL` sets below.

## API process limits

Only the API reads these (`ApiSettings`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_API_CAPACITY_REDIS_URL` | None | Redis for rate and concurrency limits shared across API replicas; required unless `HARBORRAG_ENV=dev` |
| `HARBORRAG_API_REQUESTS_PER_MINUTE` | `60` | Requests per minute per authenticated principal |
| `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` | `4` | Concurrent requests per principal |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Time limit of one request handler |

## Model gateway environment

HTTP and CLI chat, ingestion and retrieval share `HARBORRAG_MODEL_CONFIG_PATH`. The
checked-in `config/models/models.yaml` sends chat, embedding and reranking through one
LiteLLM proxy gateway and references these protected values:

| Variable | Meaning |
| --- | --- |
| `HARBORRAG_URL` | Base URL of the gateway, without `/v1`, for example `https://gateway.example.com` |
| `HARBORRAG_API_KEY` | Gateway API key, used for chat, embedding and reranking; not an API-process setting despite its name |
| `HARBORRAG_CHAT_MODEL` | Gateway model name for the `primary` chat model |
| `HARBORRAG_EMBED_MODEL` | Gateway model name for the `primary` embedding model |
| `HARBORRAG_RERANKER_MODEL` | Gateway model name for the `primary` rerank model |

The three model values are the gateway's own model names, as
`GET <HARBORRAG_URL>/v1/models` lists them, without a provider prefix. All five are
required, even while reranking is off, and changing `HARBORRAG_EMBED_MODEL` needs a
re-index; see [The shipped gateway catalog](model-config.md#the-shipped-gateway-catalog).

Keep the populated values in the git-ignored `env/.env.models` file or a secret
manager. The catalog loader does not read `.env` files: in a checkout the CLI loads
`env/.env.models` itself, and Compose passes it to the API, worker and MCP
containers.

## MCP runtime environment

The MCP server keeps transport and tool policy separate from model policy:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_MCP_CONFIG_PATH` | `config/mcp/server.yaml` | Versioned tool defaults, limits, enablement and tenant overrides |
| `HARBORRAG_MCP_AUDIT_PATH` | `.harborrag/mcp-audit.jsonl` | Owner-only audit output |
| `HARBORRAG_MCP_BEARER_TOKEN` | None | Local HTTP owner token; at least 32 bytes |
| `HARBORRAG_MCP_HOST` | `127.0.0.1` | Loopback HTTP bind address |
| `HARBORRAG_MCP_PORT` | `8010` | Local HTTP and status UI port |
| `HARBORRAG_MCP_PATH` | `/mcp` | Streamable HTTP MCP path |

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
