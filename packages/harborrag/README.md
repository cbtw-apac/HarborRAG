# harborrag

The Python SDK of [HarborRAG](https://github.com/cbtw-apac/HarborRAG): ingest your
sources, search them, walk the knowledge graph and chat over them from your own code.
`harborrag` is the public facade and install bundle of the HarborRAG packages. Importing it
does not create provider clients or open network connections.

- Documentation: <https://github.com/cbtw-apac/HarborRAG/tree/main/docs>
- Source: <https://github.com/cbtw-apac/HarborRAG>

New to HarborRAG? Start with the
[Quick Start](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/quick-start.md),
which runs HarborRAG from a clone of the repository. The `harborrag` operator command runs
from that checkout; this package is for using HarborRAG as a Python library.

## Install

```bash
pip install "harborrag[local]"
```

The `local` extra adds the clients the SDK needs to work with the local HarborRAG stack:
Qdrant, FalkorDB, S3, the model client, chunking, the control plane, parsers and tables.

Release 2.0.0a2 on PyPI predates the documentation on `main`, so commands and settings
described there may be missing from it or behave differently.

A bare `pip install harborrag` installs the runtime and the public facade but no
third-party provider clients. Other extras add one provider client each (`qdrant`,
`falkordb`, `postgres`, `s3`, `redis`), the model client for chat, embeddings and
reranking (`chat`), the Temporal client (`temporal`), the HTTP API (`server`) or the MCP
server (`mcp`). `all` adds all of them. See
[Choose the extras you need](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/installation.md#choose-the-extras-you-need).

## Quick start

The SDK works against a running HarborRAG stack. Start one from a repository checkout as
in the
[Quick Start](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/quick-start.md),
then export the stack's settings into your script's environment, as listed in
[Python SDK: Before you start](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/python-sdk/README.md#before-you-start).

```python
import asyncio

from harborrag import (
    AccessContext,
    HarborRAG,
    HarborRAGConfig,
    IngestionRequest,
    RetrievalRequest,
)


async def main() -> None:
    access = AccessContext(
        principal_id="user-1", tenant_id="DEFAULT", corpus_mode="tenant_shared"
    )

    async with HarborRAG(HarborRAGConfig()) as harbor:
        await harbor.ingestion.run(
            IngestionRequest(access=access, connector_name="harborrag-workspace")
        )
        response = await harbor.retrieval.search(
            RetrievalRequest(access=access, query="deployment requirements")
        )
        for passage in response.results:
            print(passage)


asyncio.run(main())
```

- A connection is one configured data source, and its `source_key` names it in
  `IngestionRequest`. `harborrag-workspace` is the shipped local-folder connection.
- `AccessContext` names the caller (`principal_id`) and the tenant it acts in. A tenant
  is the isolation boundary that owns data. Connections imported from the repository's
  `config/sources/connectors.yaml` live in tenant `DEFAULT`, so another tenant raises
  `ConnectionNotFoundError`. Your application must authenticate the caller before it
  builds this context.
- `corpus_mode="tenant_shared"` lets this caller read every published document of the
  tenant. Without it, the search returns only documents whose imported source
  permissions grant the caller access, which is none on a fresh stack.

[Python SDK](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/python-sdk/README.md)
explains each part.

## What it provides

- `HarborRAG`, an async context manager with the service facades `ingestion`,
  `retrieval`, `graph`, `chat`, `knowledge` and `memory`.
- `HarborRAGConfig` and `ExecutionMode`, which pick inline (`direct`, the default) or
  durable (`temporal`) execution. `HarborRAG.from_config(path)` loads them from a YAML
  file with `execution_mode`, `discover_plugins` and `runtime` keys.
- Request and response types: `AccessContext`, `IngestionRequest`, `IngestionResult`,
  `IngestionStatus`, `IngestionTaskReference`, `RetrievalRequest`, `RetrievalResponse`,
  `RetrievalLane`, the graph path, subgraph and triplet requests and queries, and the chat
  types `HarborChatRequest`, `HarborChatMessage`, `HarborChatResponse` and `ChatPrompt`.

## Chat and durable ingestion

`harbor.chat.complete(...)` and `harbor.chat.stream(...)` answer with the model configured
in `config/models/models.yaml`. They need the `chat` extra or one that includes it
(`local`, `server` or `all`). See
[Python SDK: Chat](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/python-sdk/README.md#chat).

`ingestion.run` works inline by default. `submit`, `status`, `pause`, `resume` and
`cancel` need `execution_mode: temporal`, the `temporal` extra and a reachable Temporal
server. Under `direct` they raise `ExecutionCapabilityError`; without the extra, the
Temporal import fails with `MissingOptionalDependencyError`, which names the extra to
install. See
[Python SDK: Ingestion](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/python-sdk/README.md#ingestion).

## Related packages

`harborrag` re-exports stable APIs from the other HarborRAG packages. Install one of them
directly for a narrower dependency tree; see
[Install individual packages](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/installation.md#install-individual-packages).

## Contributing

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md). Tests
for this package live in `packages/harborrag/tests/`; run them from the repository root:

```bash
uv run pytest packages/harborrag/tests
```

## License

Apache License 2.0.
