# Engine and ingestion runtime configuration

Look up the engine's two Python settings objects and the `HARBORRAG_*` environment
variables the ingestion worker, the API, chat and the MCP server read. Connector, parser
and model settings have their own file loaders; the engine itself uses two small frozen
dataclasses constructed in Python.

## `EngineConfig`

```python
from harborrag_engine.config import EngineConfig

config = EngineConfig(tenant="default", environment="local")
```

| Field | Default | Meaning |
| --- | --- | --- |
| `tenant` | `default` | Tenant label included in engine diagnostics |
| `environment` | `local` | Free-form environment label included in diagnostics |

## `EnginePolicy`

```python
from harborrag_engine.policy import EnginePolicy

policy = EnginePolicy(max_concurrency=4, retrieval_top_k=10)
```

| Field | Default | Meaning |
| --- | --- | --- |
| `max_concurrency` | `4` | Engine concurrency policy; must be at least one |
| `retrieval_top_k` | `10` | Default retrieval result count |

`CompositionRoot.diagnostics()` exposes the configured engine environment,
tenant, and concurrency values. `CompositionRoot.production()` assembles those
engine settings alongside the control-plane repositories.

## Ingestion runtime environment

The built-in worker composition loads the connector, parser and model catalogs and
the repository settings through `HARBORRAG_*` variables.

The last four rows of the table below, `HARBORRAG_API_CAPACITY_REDIS_URL`,
`HARBORRAG_API_REQUESTS_PER_MINUTE`, `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` and
`HARBORRAG_API_REQUEST_TIMEOUT_SECONDS`, are API-process settings (`ApiSettings`), not
worker settings. Only the API reads them.

| Variable | Default |
| --- | --- |
| `HARBORRAG_CONNECTOR_CONFIG_PATH` | `config/sources/connectors.yaml` |
| `HARBORRAG_PARSER_CONFIG_PATH` | `config/ingestion/parsers.yaml` |
| `HARBORRAG_CHUNKING_CONFIG_PATH` | `config/ingestion/chunking.yaml`; optional, built-in defaults apply when the default path is absent ([Chunking Configuration](chunking-config.md)) |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models/models.yaml` |
| `HARBORRAG_CONTROL_DB_URL` | Local SQLite development database |
| `HARBORRAG_OBJECT_STORE_ENDPOINT_URL` | `http://localhost:9000` |
| `HARBORRAG_OBJECT_STORE_ALLOW_INSECURE_REMOTE` | Development-only acknowledgement for plaintext non-loopback object storage; ignored in production |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` |
| `HARBORRAG_FALKORDB_HOST` | `localhost` |
| `HARBORRAG_FALKORDB_PORT` | `6379` |
| `HARBORRAG_FALKORDB_GRAPH` | `harborrag` |
| `HARBORRAG_FALKORDB_MAX_CONNECTIONS` | `32` |
| `HARBORRAG_GRAPH_RELATION_REPAIR_CONCURRENCY` | `8` |
| `HARBORRAG_RETIRED_VERSION_RETENTION_DAYS` | `30`; days a retired document version keeps its object-store artifacts after projection cleanup (`0` purges at once, `off` keeps them) |
| `HARBORRAG_RETIRED_VERSION_PURGE_INTERVAL_HOURS` | `24`; how often each ingestion worker sweeps every scope for expired retired versions (`0` disables the sweep; the purge after each source run still applies) |
| `HARBORRAG_SCHEDULE_CONFIG_PATH` | `config/sources/schedules.yaml`; declarative schedules the API syncs at startup (the API image sets `/app/config/sources/schedules.yaml`) |
| `HARBORRAG_SCHEDULE_METRICS_INTERVAL_SECONDS` | `30`; how often a worker exports schedule pause and overlap gauges (`0` disables; with several worker replicas enable it on one) |
| `HARBORRAG_REDIS_URL` | Optional disposable cache/rate limiter |
| `HARBORRAG_REDIS_ALLOW_INSECURE_REMOTE` | Development-only acknowledgement for plaintext non-loopback Redis; ignored in production |
| `HARBORRAG_API_CAPACITY_REDIS_URL` | Required in production for cross-replica API rate and concurrency limits |
| `HARBORRAG_API_REQUESTS_PER_MINUTE` | `60` per authenticated principal |
| `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` | `4` |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` |

Database URLs, API keys, object-store credentials, and FalkorDB passwords are
secret settings. They are masked by `RuntimeSettings` and must be supplied
through the protected deployment environment rather than checked-in files.

The embedding model and its dimensions come from the selected model catalog. Set
`HARBORRAG_EMBEDDING_MODEL` or `HARBORRAG_EMBEDDING_DIMENSIONS` only when the catalog
is ambiguous or a non-default model is required. `HARBORRAG_EMBEDDING_MODEL` names a
logical model of the catalog's `embed` section, such as `primary`; it is not the
gateway's model name, which `HARBORRAG_EMBED_MODEL` sets below.

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
`GET <HARBORRAG_URL>/v1/models` lists them, without a provider prefix.

All five are required. References expand eagerly at load time, so a missing reranker
variable fails exactly as hard as a missing chat one. Changing `HARBORRAG_EMBED_MODEL`
needs a re-index; see [Model configuration](model-config.md#the-shipped-gateway-catalog).

Store populated values in the ignored `env/.env.models` file or an external
secret manager. HarborRAG's configuration loader does not search `.env` files;
the deployment scripts and Compose services explicitly inject that file.

## MCP runtime environment

The MCP server keeps transport/tool policy separate from model policy:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_MCP_CONFIG_PATH` | `config/mcp/server.yaml` | Versioned tool defaults, limits, enablement, and tenant overrides |
| `HARBORRAG_MCP_AUDIT_PATH` | `.harborrag/mcp-audit.jsonl` | Owner-only audit output |
| `HARBORRAG_MCP_BEARER_TOKEN` | None | Local HTTP owner token; at least 32 bytes |
| `HARBORRAG_MCP_HOST` | `127.0.0.1` | Loopback HTTP bind address |
| `HARBORRAG_MCP_PORT` | `8010` | Local HTTP/UI port |
| `HARBORRAG_MCP_PATH` | `/mcp` | Streamable HTTP MCP path |

`scripts/deployment/dev.sh bootstrap` creates the ignored `env/.env.mcp` file,
generates its bearer token, and restricts the file to its owner. Tool settings
can be edited in `config/mcp/server.yaml` or through the authenticated local UI, but
the UI intentionally cannot edit model credentials or provider endpoints.

See [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).
