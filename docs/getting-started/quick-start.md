# Quick Start

## 1. Install

```bash
uv sync --all-packages --extra dev
```

## 2. Inspect the application CLI

```bash
uv run --package harborrag-app harborrag --help
```

This confirms the console entry point without requiring Temporal or data
services. `harborrag doctor` is a live Temporal health check, so run it after
starting the services below.

## 3. Load the catalogs

The checked-in YAML files can be inspected through the runtime loaders:

```bash
LOCAL_SOURCE_PATH=docs uv run python -c "from harborrag_runtime.config import load_connector_catalog; c = load_connector_catalog('config/connectors.example.yaml'); print(c.names(enabled_only=True)); print(list(c.build_enabled()))"
uv run python -c "from harborrag_runtime.config import load_parser_catalog; c = load_parser_catalog('config/parsers.yaml'); print(c.names(enabled_only=True))"
```

## 4. Start the ingestion services and worker

```bash
scripts/deployment/dev.sh data
# Configure config/models.yaml and env/.env.models first.
scripts/deployment/dev.sh temporal
scripts/deployment/dev.sh worker
```

This starts Qdrant, FalkorDB, PostgreSQL-backed Temporal, the `harborrag`
namespace, UI, and the configured HarborRAG worker. Then submit a run:

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run --package harborrag-app harborrag doctor --json

LOCAL_SOURCE_PATH=docs HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run --package harborrag-app harborrag ingest start \
  --tenant tenant-1 --connector harborrag-workspace --wait
```

See [Deployment](../developers/deployment/README.md) for the complete local
service and worker configuration sequence.

## 5. Try chat (optional)

Chat makes a real provider request and may incur provider charges. Configure
the protected model environment before running it:

```bash
cp env-example/.env.models.example env/.env.models
# Replace the HARBOR_CHAT_* placeholders, then load the file.
set -a
source env/.env.models
set +a

uv run --package harborrag-app harborrag chat \
  "Explain HarborRAG in one paragraph." --json
```

This is a non-streaming, retrieval-grounded completion. Its JSON response
contains a generated session ID; pass that ID with `--session` on later calls
to recall recent conversation turns. See the
[Chat guide](../users/chat/README.md) for HTTP and CLI usage.

## 6. Run tests

```bash
uv run pytest
uv run make coverage
```

## Continue

- [Connector, parser, and model configuration](../users/configuration/README.md)
- [Chat](../users/chat/README.md)
- [CLI Reference](../users/cli-reference/README.md)
- [Architecture](../developers/architecture/README.md)
- [Testing](../developers/testing/README.md)
