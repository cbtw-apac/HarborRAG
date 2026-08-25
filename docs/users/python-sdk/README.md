# Python SDK

HarborRAG is a library first. `HarborRAG` is an async context manager that
exposes four service facades — `ingestion`, `retrieval`, `graph`, and `chat` —
over one configured runtime.

## Install

```bash
pip install "harborrag[local]"
```

`harborrag[local]` covers direct local ingestion and retrieval. Add `chat` for
completions, `temporal` for durable runs, or use `all`. See
[Installation](../../getting-started/installation.md).

## Construct the runtime

```python
import asyncio

from harborrag import AccessContext, HarborRAG, IngestionRequest, RetrievalRequest


async def main() -> None:
    access = AccessContext(principal_id="user-1", tenant_id="tenant-1")

    async with HarborRAG.from_config("config/harborrag.example.yaml") as harbor:
        await harbor.ingestion.run(
            IngestionRequest(access=access, connector_name="harborrag-workspace")
        )
        response = await harbor.retrieval.search(
            RetrievalRequest(access=access, query="deployment requirements")
        )
        for result in response.results:
            print(result)


asyncio.run(main())
```

`from_config` reads `execution_mode`, `discover_plugins`, and `runtime`
settings. Entering the context starts the executor; leaving it closes the
runtime. Every call carries an `AccessContext`, which is how tenancy and
authorization are enforced — there is no ambient tenant.

## Ingestion

Connectors are declared once in
[`config/connectors.yaml`](../configuration/connector-config.md) and selected by
name, so credentials stay as environment references rather than literals in
code.

```python
request = IngestionRequest(access=access, connector_name="confluence-main")

result = await harbor.ingestion.run(request)          # execute inline
```

For long runs, submit durably and control the workflow:

```python
task = await harbor.ingestion.submit(request)         # -> IngestionTaskReference
status = await harbor.ingestion.status(task.task_id)  # -> IngestionStatus

await harbor.ingestion.pause(task.task_id)
await harbor.ingestion.resume(task.task_id)
await harbor.ingestion.cancel(task.task_id)
```

`submit`, `status`, `pause`, `resume`, and `cancel` require
`execution_mode: temporal` and the `harborrag[temporal]` extra. `run` executes
directly and needs neither.

`IngestionRequest` also accepts scoping and tuning fields: `connection_id`,
`source_scope_id`, `path`, `pattern`, `recursive`, `updated_after`, `limit`,
`include_attachments`, `filters`, `force_reprocess`, `discovery_page_size`,
`discovery_concurrency`, and `document_concurrency`.

To follow progress, poll `status(task_id)` — its payload carries the stage
sequence and a `progress` mapping. The CLI's `harborrag ingest watch` renders
exactly that payload on a refresh interval.

## Retrieval

```python
from harborrag import RetrievalLane

response = await harbor.retrieval.search(
    RetrievalRequest(
        access=access,
        query="how is chunking configured",
        top_k=10,
        lane=RetrievalLane.HYBRID,
        observe_graph=True,
    )
)
print(response.lane, response.request_id)
print(response.diagnostics)
```

Lanes are `DENSE`, `SPARSE`, and `HYBRID`. Results resolve against the
authoritative active document version, so a superseded version is never
returned even while a reindex is in flight.

## Graph

```python
from harborrag import GraphPathRequest, GraphSubgraphRequest, GraphTripletRequest

triplets = await harbor.graph.search_triplets(GraphTripletRequest(...))
paths = await harbor.graph.find_paths(GraphPathRequest(...))
subgraph = await harbor.graph.expand_subgraph(GraphSubgraphRequest(...))
```

Each response carries its payload plus `diagnostics`.

## Chat

```python
from harborrag import ChatPrompt, HarborChatMessage, HarborChatRequest

reply = await harbor.chat.complete(
    HarborChatRequest(messages=(HarborChatMessage.user("Summarize the results"),)),
    prompt=ChatPrompt.CONCISE,
)
```

Prompts are `ChatPrompt.DEFAULT` and `ChatPrompt.CONCISE`. Chat needs a model
client, so install `harborrag[chat]` (or `local`/`server`/`all`) and configure
`config/models.yaml`. Chat makes a real provider request and may incur charges.

## Building connectors in code

`ConnectorDefinition` is a frozen dataclass and can be constructed directly,
which is useful for inspecting or validating a connector recipe:

```python
from harborrag_runtime.config import ConnectorDefinition

definition = ConnectorDefinition(
    name="confluence-01",
    provider="confluence",
    settings={"deployment_type": "cloud", "space_key": "ENG"},
    setting_environment={"base_url": "CONFLUENCE_BASE_URL"},
    secret_environment={"token": "CONFLUENCE_TOKEN"},
)

resolved = definition.resolve_settings()
```

`resolve_settings()` applies provider defaults, then literal settings, then
referenced environment values, then explicit overrides, and fails if a
referenced variable is missing or empty.

Note that `IngestionRequest` selects a connector by **name** from the loaded
catalog, so a definition built this way is not yet passed to `ingestion.run`
directly.

## What needs backing services

Constructing `HarborRAG.from_config(...)` and building request objects need no
services. Actually running ingestion, retrieval, graph, or chat calls needs the
configured stores and model providers — see
[Deployment](../../developers/deployment/README.md).

## Related

- [Installation](../../getting-started/installation.md)
- [Connector configuration](../configuration/connector-config.md)
- [Model configuration](../configuration/model-config.md)
- [CLI reference](../cli-reference/README.md)
- [Chat](../chat/README.md)
