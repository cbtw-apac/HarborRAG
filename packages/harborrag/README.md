# harborrag

`harborrag` is the public SDK facade and installation bundle for
[HarborRAG](https://github.com/cbtw-apac/HarborRAG), a modular, provider-agnostic RAG
framework for engineering knowledge. Importing it does not initialize provider clients or
perform network I/O.

- Documentation: <https://cbtw-apac.github.io/HarborRAG/>
- Source: <https://github.com/cbtw-apac/HarborRAG>

## Install

```bash
pip install "harborrag[all]"          # everything
pip install "harborrag[local]"        # local end-to-end ingestion and retrieval
pip install "harborrag[cli,qdrant]"   # or just what you need
```

A bare `pip install harborrag` installs the framework - contracts, engine, memory,
adapters, and runtime - but deliberately **no third-party provider clients**. There is no
vector store, no graph store, and no model client until you add an extra:

| Extra | Adds |
| --- | --- |
| `local` | Qdrant, FalkorDB, S3, model client, chunking, control plane, parsers, Docling PDF, tables |
| `chat` | the model client used by chat, embeddings, and reranking |
| `cli` | the `harborrag` command |
| `server` | the HTTP API plus the production and Temporal runtime |
| `mcp` | the MCP transport |
| `temporal` | the Temporal client for durable ingestion |
| `qdrant`, `falkordb`, `postgres`, `s3`, `redis` | one provider each |
| `all` | everything above; a superset of `local` |

Conversation memory (`harborrag-memory`) is already a required dependency, so it is
available in every install. The `harborrag[memory]` extra exists for explicitness and adds
nothing new.

See [Installation](https://cbtw-apac.github.io/HarborRAG/docs/getting-started/installation.html)
for PDF/OCR backends, editable installs, and platform notes.

## Ingest and retrieve

`HarborRAG` exposes four async service facades - `ingestion`, `retrieval`, `graph`, and
`chat` - behind one async context manager. Connectors are declared by name in your
connector catalog, so credentials stay as environment references:

```python
import asyncio

from harborrag import AccessContext, HarborRAG, IngestionRequest, RetrievalRequest


async def main() -> None:
    access = AccessContext(principal_id="user-1", tenant_id="tenant-1")

    async with HarborRAG.from_config("config/harborrag.example.yaml") as harbor:
        await harbor.ingestion.run(
            IngestionRequest(access=access, connector_name="harborrag-workspace")
        )
        results = await harbor.retrieval.search(
            RetrievalRequest(access=access, query="deployment requirements")
        )
        print(results)


asyncio.run(main())
```

`config/harborrag.example.yaml` ships in the repository checkout. Copy it next to your
application and point `from_config` at your own path.

## Chat

Chat needs a model provider, so install `harborrag[chat]` (or any extra that includes it,
such as `local`, `server`, or `all`) and configure credentials first:

```python
from harborrag import ChatPrompt, HarborChatMessage, HarborChatRequest, HarborRAG

async with HarborRAG.from_config("config/harborrag.example.yaml") as harbor:
    response = await harbor.chat.complete(
        HarborChatRequest(messages=(HarborChatMessage.user("Summarize the results"),)),
        prompt=ChatPrompt.CONCISE,
    )
```

## Direct versus durable execution

Direct execution is the default: `ingestion.run` does the work inline.

The durable controls - `submit`, `status`, `pause`, `resume`, and `cancel` - need
`execution_mode: temporal` in your configuration **and** the `harborrag[temporal]` extra.
Calling them without both raises `ExecutionCapabilityError`:

```python
task = await harbor.ingestion.submit(request)
status = await harbor.ingestion.status(task.task_id)
await harbor.ingestion.pause(task.task_id)
await harbor.ingestion.resume(task.task_id)
await harbor.ingestion.cancel(task.task_id)
```

## Related packages

`harborrag` re-exports stable APIs from the workspace packages. Install one directly for a
narrower dependency tree:

| Package | Contains |
| --- | --- |
| `harborrag-core` | provider-neutral contracts and domain models |
| `harborrag-adapters` | connectors, parsers, model clients, repositories |
| `harborrag-engine` | ingestion and retrieval orchestration |
| `harborrag-memory` | scope-aware conversation memory |
| `harborrag-runtime` | production composition and Temporal orchestration |
| `harborrag-app` | CLI and HTTP API |
| `harborrag-mcp-server` | MCP tools and transport |

## Development

Tests for this package live in `packages/harborrag/tests/`. Run them from the repository
root:

```shell
uv run pytest packages/harborrag/tests
```

Licensed under the Apache License 2.0.
