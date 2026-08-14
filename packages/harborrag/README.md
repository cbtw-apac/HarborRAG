# HarborRAG

`harborrag` is the lightweight public SDK facade and installation bundle for
HarborRAG. Importing it does not initialize provider clients or perform network I/O.

Install only the facade and contracts:

```bash
pip install harborrag
```

Install the provider bundle for direct local ingestion:

```bash
pip install "harborrag[local]"
```

```python
from harborrag import (
    AccessContext,
    HarborRAG,
    IngestionRequest,
    RetrievalRequest,
)

access = AccessContext(principal_id="user-1", tenant_id="tenant-1")
async with HarborRAG.from_config("harborrag.yaml") as harbor:
    ingestion = await harbor.ingestion.run(IngestionRequest(access=access, connector_name="local"))
    results = await harbor.retrieval.search(
        RetrievalRequest(access=access, query="deployment requirements")
    )
```

Chat completion uses a separately installable model provider:

```bash
pip install "harborrag[chat]"
```

```python
from harborrag import (
    ChatPrompt,
    HarborChatMessage,
    HarborChatRequest,
    HarborRAG,
)

async with HarborRAG.from_config("harborrag.yaml") as harbor:
    response = await harbor.chat.complete(
        HarborChatRequest(messages=(HarborChatMessage.user("Summarize the results"),)),
        prompt=ChatPrompt.CONCISE,
    )
```

Direct execution is the default. Temporal-only controls such as `submit`, `status`,
`pause`, `resume`, and `cancel` require `execution_mode: temporal` and the
`harborrag[temporal]` extra.

Additional bundles are intentionally explicit:

- `harborrag[chat]` installs the model client used by chat completion.
- `harborrag[cli]` installs the CLI.
- `harborrag[server]` installs API and production runtime dependencies.
- `harborrag[mcp]` installs the MCP transport.
- `harborrag[qdrant]`, `harborrag[falkordb]`, `harborrag[postgres]`,
  `harborrag[s3]`, and `harborrag[redis]` install individual providers.
- `harborrag[all]` installs the server, Temporal, MCP, and all parser providers.

## Development

Tests for this package live in:

```text
packages/harborrag/tests/
```

Run from the repository root:

```shell
uv run pytest packages/harborrag/tests
```
