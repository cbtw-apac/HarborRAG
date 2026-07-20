# Quick Start

This walks through installing the workspace and exercising the deterministic local ingestion check. No external services are required.

## 1. Install the workspace

```bash
uv sync --all-packages --extra dev
```

See [Installation](installation.md) for the pip-based alternative and troubleshooting.

## 2. Run the CLI doctor command

```bash
python -m harborrag_app.cli.main doctor --json
```

```json
{"diagnostics": {"engine": {"environment": "local", "max_concurrency": 4, "tenant": "default"}, "runtime": {"provider": "mock_runtime", "ready": true}}, "ok": true}
```

This confirms the runtime and engine composition (`harborrag_runtime.composition.CompositionRoot`) wires up correctly.

## 3. Run the local ingestion check

```bash
python scripts/run_mock_pipeline.py --json
```

This connects the runtime's deterministic in-memory source to the real text parser and reports the resulting ingestion summary. It is a composition check, not a live repository smoke test:

```json
{
  "documents": [{"id": "mock://composition/1", "source": "mock://composition/1", "content_type": "text/plain", "text": "..."}],
  "chunks": [{"id": "mock://composition/1#chunk-0", "document_id": "mock://composition/1", "text": "..."}],
  "retrieval": [{"id": "mock://composition/1#chunk-0", "text": "...", "score": 1.0, "metadata": {"document_id": "mock://composition/1"}}],
  "summary": {"discovered": 1, "loaded": 1, "parsed": 1, "indexed": 0}
}
```

## 4. Call the MCP mock tools

```python
from harborrag_mcp.server import MockMcpServer

server = MockMcpServer()
print(server.call_tool("harbor_health_check"))
print(server.call_tool("harbor_sample_retrieve", {"query": "HarborRAG"}))
```

See [MCP Mock Tools](../users/detailed-guides/mcp-server/README.md) for the full tool list and [Setup & Integration](../users/detailed-guides/mcp-server/setup-and-integration.md) for wiring a client to the server.

## 5. Run the test suite

```bash
pytest
pytest --cov --cov-report=term-missing
```

Every package owns its own `tests/` folder; coverage must stay at or above 90% (`make coverage`).

## Where to go next

- [What is HarborRAG?](what-is-harborrag.md) — the framework's architecture and current status.
- [Architecture Overview](../developers/architecture/README.md) — package map and dependency rules.
- [Extending HarborRAG](../developers/extending/README.md) — implement a real connector, parser, model, or repository.
- [CLI Reference](../users/cli-reference/README.md) — current and planned CLI commands.
