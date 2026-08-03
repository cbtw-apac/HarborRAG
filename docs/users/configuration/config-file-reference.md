# Engine and ingestion runtime configuration

Connector, parser, and model settings have file loaders. The engine itself currently uses two small frozen dataclasses constructed in Python.

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

The built-in worker composition loads the three catalogs and repository
settings through `HARBORRAG_*` variables:

| Variable | Default |
| --- | --- |
| `HARBORRAG_CONNECTOR_CONFIG_PATH` | `config/connectors.yaml` |
| `HARBORRAG_PARSER_CONFIG_PATH` | `config/parsers.yaml` |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models.yaml` |
| `HARBORRAG_CONTROL_DB_URL` | Local SQLite development database |
| `HARBORRAG_OBJECT_STORE_ENDPOINT_URL` | `http://localhost:9000` |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` |
| `HARBORRAG_FALKORDB_HOST` | `localhost` |
| `HARBORRAG_FALKORDB_PORT` | `6379` |
| `HARBORRAG_FALKORDB_GRAPH` | `harborrag` |
| `HARBORRAG_FALKORDB_MAX_CONNECTIONS` | `32` |
| `HARBORRAG_GRAPH_RELATION_REPAIR_CONCURRENCY` | `8` |
| `HARBORRAG_REDIS_URL` | Optional disposable cache/rate limiter |

Database URLs, API keys, object-store credentials, and FalkorDB passwords are
secret settings. They are masked by `RuntimeSettings` and must be supplied
through the protected deployment environment rather than checked-in files.

Embedding model and dimensions are derived from the selected model catalog.
Set `HARBORRAG_EMBEDDING_MODEL` or `HARBORRAG_EMBEDDING_DIMENSIONS` only when
the catalog is ambiguous or a non-default logical model is required.

## Chat model environment

HTTP, CLI, and MCP chat share `HARBORRAG_MODEL_CONFIG_PATH`. The checked-in
`config/models.yaml` references these protected values:

| Variable | Meaning |
| --- | --- |
| `HARBOR_CHAT_PROVIDER` | Provider identifier allowed by the chat security policy |
| `HARBOR_CHAT_MODEL` | Provider model identifier for the `primary` deployment |
| `HARBOR_CHAT_API_KEY` | Provider credential |

Store populated values in the ignored `env/.env.models` file or an external
secret manager. HarborRAG's configuration loader does not search `.env` files;
the deployment scripts and Compose services explicitly inject that file.

## MCP runtime environment

The MCP server keeps transport/tool policy separate from model policy:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBORRAG_MCP_CONFIG_PATH` | `config/mcp.yaml` | Versioned tool defaults, limits, enablement, and tenant overrides |
| `HARBORRAG_MCP_AUDIT_PATH` | `.harborrag/mcp-audit.jsonl` | Owner-only audit output |
| `HARBORRAG_MCP_BEARER_TOKEN` | None | Local HTTP owner token; at least 32 bytes |
| `HARBORRAG_MCP_HOST` | `127.0.0.1` | Loopback HTTP bind address |
| `HARBORRAG_MCP_PORT` | `8010` | Local HTTP/UI port |
| `HARBORRAG_MCP_PATH` | `/mcp` | Streamable HTTP MCP path |

`scripts/deployment/dev.sh bootstrap` creates the ignored `env/.env.mcp` file,
generates its bearer token, and restricts the file to its owner. Tool settings
can be edited in `config/mcp.yaml` or through the authenticated local UI, but
the UI intentionally cannot edit model credentials or provider endpoints.

See [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).
