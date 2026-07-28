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
| `HARBORRAG_CONNECTOR_CONFIG_PATH` | `config/connectors.example.yaml` |
| `HARBORRAG_PARSER_CONFIG_PATH` | `config/parsers.yaml` |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models.example.yaml` |
| `HARBORRAG_INGESTION_STATE_DATABASE` | `.harborrag/ingestion-state.db` |
| `HARBORRAG_INGESTION_OBJECT_ROOT` | `.harborrag/objects` |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` |
| `HARBORRAG_FALKORDB_HOST` | `localhost` |
| `HARBORRAG_FALKORDB_PORT` | `6379` |
| `HARBORRAG_VECTOR_COLLECTION` | `harborrag_chunks` |
| `HARBORRAG_GRAPH_NAMESPACE` | `harborrag` |

Embedding model and dimensions are derived from the selected model catalog.
Set `HARBORRAG_EMBEDDING_MODEL` or `HARBORRAG_EMBEDDING_DIMENSIONS` only when
the catalog is ambiguous or a non-default logical model is required.
