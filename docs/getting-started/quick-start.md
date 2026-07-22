# Quick Start

## 1. Install

```bash
uv sync --all-packages --extra dev
```

## 2. Check the development application service

```bash
uv run python -m harborrag_app.cli.main doctor --json
```

The command returns an `ok: true` development diagnostic. It does not start a
production ingestion worker or external services.

## 3. Load the catalogs

The checked-in YAML files can be inspected through the runtime loaders:

```bash
LOCAL_SOURCE_PATH=docs uv run python -c "from harborrag_runtime.config import load_connector_catalog; c = load_connector_catalog('config/connectors.example.yaml'); print(c.names(enabled_only=True)); print(list(c.build_enabled()))"
uv run python -c "from harborrag_runtime.config import load_parser_catalog; c = load_parser_catalog('config/parsers.yaml'); print(c.names(enabled_only=True))"
```

## 4. Start the ingestion services and worker

```bash
DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh
# Configure config/models.yaml and env/.env.models first.
# Then set TEMPORAL_START_WORKER=1 and the config file paths in env/.env.temporal.
scripts/deployment/temporal_up.sh
```

This starts Qdrant, FalkorDB, PostgreSQL-backed Temporal, the `harborrag`
namespace, UI, and the configured HarborRAG worker. Then submit a run:

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run --package harborrag-app harbor ingest start \
  --tenant tenant-1 --connector local-docs --wait
```

See [Temporal deployment](../../deploy/temporal/README.md) for the complete
configuration sequence.

## 5. Run tests

```bash
uv run pytest
uv run make coverage
```

## Continue

- [Connector, parser, and model configuration](../users/configuration/README.md)
- [CLI Reference](../users/cli-reference/README.md)
- [Architecture](../developers/architecture/README.md)
- [Testing](../developers/testing/README.md)
