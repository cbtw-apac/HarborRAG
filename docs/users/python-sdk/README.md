# Python SDK

Ingest sources, retrieve passages, walk the knowledge graph and chat from your own
Python code. `HarborRAG` is an async context manager that exposes service facades over
one configured runtime: `ingestion`, `retrieval`, `graph` and `chat`, plus `knowledge`
for evidence and document reads and `memory` for conversation context.

## Before you start

- A repository checkout with the local stack running, set up as in the
  [Quick Start](../../getting-started/quick-start.md). The checkout's workspace already
  includes every HarborRAG package, so run your scripts from the checkout root with
  `uv run python <your-script>.py`. Installing the packages into another environment is
  described in [Installation](../../getting-started/installation.md).
- A connection to ingest. A connection is one configured data source, and its
  `source_key` is the name the SDK uses for it; see
  [Connections](../connections.md#terms-used-on-this-page).
- The stack's settings in the script's environment. The SDK reads `HARBORRAG_*`
  variables from the process environment and the `runtime:` block of a configuration
  file you pass; unlike the CLI, it loads no `.env` file and none of the checkout's
  `env/` files (listed in
  [Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout)).
  Export at least `HARBORRAG_CONTROL_DB_URL` and `HARBORRAG_SECRETS_ENCRYPTION_KEY`
  with the stack's values, the object-store keys `HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID`
  and `HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY` (the stack's MinIO root user and
  password), and the model gateway variables of `env/.env.models`.

Without `HARBORRAG_CONTROL_DB_URL`, the SDK keeps connections and run state in a local
SQLite file, `./harborrag_control.db`, while it reads and writes the same vector, graph
and object stores as the stack. The CLI refuses that split in a checkout; do not ingest
that way.

## Ingest and search

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
        result = await harbor.ingestion.run(
            IngestionRequest(access=access, connector_name="harborrag-workspace")
        )
        print(result.status, result.discovered, result.published, result.failed)

        response = await harbor.retrieval.search(
            RetrievalRequest(access=access, query="deployment requirements")
        )
        for passage in response.results:
            print(passage)


asyncio.run(main())
```

What each part does:

- `HarborRAGConfig()` takes its settings from the environment and runs ingestion inline
  (`execution_mode: direct`). `HarborRAG.from_config("<path>.yaml")` loads a file with
  `execution_mode`, `discover_plugins` and `runtime` keys instead, and rejects any other
  key. Entering the context starts the executor; leaving it closes the runtime.
- `AccessContext` names the caller (`principal_id`) and the tenant it acts in
  (`tenant_id`); there is no ambient tenant. A tenant is the isolation boundary that owns
  data; see [Tenant and workspace status](../configuration/workspace-mode.md). The SDK
  trusts the context it is given, so your application must authenticate the caller and
  check its tenant before it builds one.
- `corpus_mode="tenant_shared"` lets this caller read every published document of the
  tenant. Without it, the search returns only documents the caller has imported
  permission snapshots for, which is nothing on a fresh stack; see
  [Retrieval](#retrieval).
- `harborrag-workspace` is the `source_key` of the shipped local-folder connection when
  it was imported from `config/sources/connectors.yaml`. On a stack whose connections
  existed before database migration 0041, the key is `default/harborrag-workspace`.

## Ingestion

`IngestionRequest` names a stored connection by its `source_key`, looked up in the
access context's tenant: `connection_id` when you set it, otherwise `connector_name`.
The connection's credentials stay in the control database, never in your code. A
connection that does not exist in that tenant raises `ConnectionNotFoundError`; the
shipped `harborrag-workspace` connection exists only in tenant `DEFAULT`. Create
connections as described in [Connections](../connections.md).

```python
request = IngestionRequest(access=access, connector_name="harborrag-workspace")

result = await harbor.ingestion.run(request)
```

`run` waits for the result, an `IngestionResult` with `task_id`, `status`, `discovered`,
`published`, `unchanged` and `failed`. Under `execution_mode: direct` it runs inline,
without Temporal; under `execution_mode: temporal` it submits the run to Temporal and
waits for it.

For long runs, submit durably and control the workflow:

```python
task = await harbor.ingestion.submit(request)
status = await harbor.ingestion.status(task.task_id)

await harbor.ingestion.pause(task.task_id)
await harbor.ingestion.resume(task.task_id)
await harbor.ingestion.cancel(task.task_id)
```

`submit`, `status`, `pause`, `resume` and `cancel` need `execution_mode: temporal` and a
reachable Temporal server (`HARBORRAG_TEMPORAL_TARGET`, default `localhost:7233`).
Under `direct` they raise `ExecutionCapabilityError`.

Pause and resume are cooperative. A pause lets document work that already started
finish, then stops the next batch from starting; poll `status(task_id)` until `paused`
is `True` before you treat the run as paused. Resume lets new batches start again.
Cancelling a paused run releases that wait and stops at the same safe boundary.

`status(task_id)` returns an `IngestionStatus` with `task_id`, `status`, `paused` and
`cancel_requested`, and nothing more: no stages and no progress counts. Its `status` is
the workflow's own state, such as `RUNNING`, not the HTTP API's run status (`queued`,
`running`, `succeeded` and so on). `harborrag ingest watch` shows a richer view because
it goes through the application service, which combines workflow status, per-stage
progress and execution status; that view is not available through the SDK.

`IngestionRequest` also accepts scoping and tuning fields: `task_id`, `connection_id`,
`source_scope_id`, `path`, `pattern`, `recursive`, `updated_after`, `limit`,
`include_attachments`, `filters`, `force_reprocess`, `discovery_page_size`,
`discovery_concurrency` and `document_concurrency`. `force_reprocess=True` is the same
switch as `"mode": "force"` on `POST /v1/admin/ingestions` and `--force-reprocess` on
the CLI; see [Ingestion modes](../ingestion-modes.md).

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

Lanes are `DENSE`, `SPARSE` and `HYBRID` (the default). Results resolve against the
authoritative active document version, so a superseded version is never returned, even
while a reindex is in flight.

`AccessContext.corpus_mode` decides which documents the search may return. The default,
`source_acl`, returns only documents the principal has current permission snapshots
for, so a fresh corpus without imported permissions returns nothing. Pass
`corpus_mode="tenant_shared"` only when your application has decided that the caller may
read every published document of the tenant. See
[Tenant and workspace status](../configuration/workspace-mode.md).

`rerank=True` reorders the passages with the `rerank` model of
`config/models/models.yaml`: retrieval fetches a larger pool, keeps the first `top_k` in
rerank order, and puts each score in `metadata["rerank_score"]`. It applies to that
request only; the `retrieval.rerank` runtime setting is read by chat, not by the SDK.
Without a `rerank` section, the request is served in retrieval order and a warning is
logged. See [Rerank chat retrieval](../configuration/runtime-settings.md#rerank-chat-retrieval).

`harbor.retrieval.find_entities(...)` ranks source entities instead of passages.

## Graph

```python
from harborrag import GraphTripletQuery, GraphTripletRequest

triplets = await harbor.graph.search_triplets(
    GraphTripletRequest(access=access, query=GraphTripletQuery(subject="<subject>", limit=10))
)
```

A triplet query needs at least one of `subject`, `predicate` or `object`.
`harbor.graph.find_paths(GraphPathRequest(...))` and
`harbor.graph.expand_subgraph(GraphSubgraphRequest(...))` follow the same pattern with
`GraphPathQuery` and `GraphSubgraphQuery`. Each request takes the access context and a
query, only readable records come back, and each response carries its payload plus
`diagnostics`.

## Chat

```python
from harborrag import ChatPrompt, HarborChatMessage, HarborChatRequest

reply = await harbor.chat.complete(
    HarborChatRequest(messages=(HarborChatMessage.user("Summarize the results"),)),
    prompt=ChatPrompt.CONCISE,
)
```

Pick the prompt with `ChatPrompt.DEFAULT` or `ChatPrompt.CONCISE`. `harbor.chat.stream`
takes the same arguments and yields the reply in chunks. Chat sends a real request to
the model provider configured in `config/models/models.yaml` (see
[Model configuration](../configuration/model-config.md)), which may incur charges.

## Build a connector definition in code

`ConnectorDefinition` is a frozen dataclass you can build directly, for example to
inspect or validate a connector entry of `connectors.yaml`:

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

`resolve_settings()` merges the literal settings, then the referenced environment
values, then explicit `overrides`, and raises `ConnectorConfigurationError` when a
referenced variable is missing or empty. It does not apply provider defaults: those come
from the provider's own configuration class when `ConnectorDefinition.build()`
constructs the connector, so the mapping it returns holds only the keys you supplied.

`ingestion.run` cannot take a definition built this way: an `IngestionRequest` names a
stored connection. To ingest it, create a connection through the API or
`harborrag source add`.

## What needs backing services

Building `HarborRAG(...)` or `HarborRAG.from_config(...)` and the request objects needs
no services. Running ingestion, retrieval, graph or chat calls needs the configured
stores and model provider; see [Deployment](../../developers/deployment/README.md).

## Related

- [Connections](../connections.md)
- [Ingestion modes](../ingestion-modes.md)
- [Model configuration](../configuration/model-config.md)
- [CLI reference](../cli-reference/README.md)
- [Chat](../chat/README.md)
- [Installation](../../getting-started/installation.md)
