# Quick Start

This path exercises only credential-free local behavior.

## 1. Install

```bash
uv sync --all-packages --extra dev
```

## 2. Check the local composition

```bash
uv run python -m harborrag_app.cli.main doctor --json
```

Expected shape:

```json
{
  "diagnostics": {
    "engine": {"environment": "local", "max_concurrency": 4, "tenant": "default"},
    "runtime": {"provider": "mock_runtime", "ready": true}
  },
  "ok": true
}
```

## 3. Run the deterministic pipeline

```bash
uv run python scripts/run_mock_pipeline.py --json --query HarborRAG --top-k 1
```

The script discovers and parses one in-memory text document, creates one demonstration chunk, and runs deterministic retrieval. Its summary is:

```json
{"discovered": 1, "loaded": 1, "parsed": 1, "indexed": 0}
```

`indexed` remains zero because this local check does not persist to a real vector repository.

## 4. Load the example catalogs

The checked-in YAML files are examples. They are not loaded automatically:

```bash
LOCAL_SOURCE_PATH=docs uv run python -c "from harborrag_runtime.config import load_connector_catalog; c = load_connector_catalog('config/connectors.example.yaml'); print(c.names(enabled_only=True)); print(list(c.build_enabled()))"
uv run python -c "from harborrag_runtime.config import load_parser_catalog; c = load_parser_catalog('config/parsers.example.yaml'); print(c.names(enabled_only=True))"
```

The connector command should show `local-docs`; the parser command should show `pdf-default`.

## 5. Call the MCP mock tools

```bash
uv run python -c "from harborrag_mcp.server import list_tools, call_tool; print(list_tools()); print(call_tool('harbor_health_check'))"
```

These functions dispatch in process. They do not start an MCP protocol server.

## 6. Run tests

```bash
uv run pytest
uv run make coverage
```

## Continue

- [Connector, parser, and model configuration](../users/configuration/README.md)
- [CLI Reference](../users/cli-reference/README.md)
- [Architecture](../developers/architecture/README.md)
- [Testing](../developers/testing/README.md)
