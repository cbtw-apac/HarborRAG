# Chat

The HTTP API and CLI chat surfaces are retrieval-grounded: every call searches
indexed HarborRAG content for the given prompt, injects the retrieved chunks
as context, and asks the model to answer from that context. Both surfaces
load the `chat` family from `config/models.yaml` and call
`AsyncHarborChatClient` through the runtime facade.

| Surface | Entry point | Best for |
| --- | --- | --- |
| HTTP API | `POST /v1/chat/completions` | Applications and authenticated services |
| HTTP API (streaming) | `POST /v1/chat/stream` | Incremental rendering as the model responds |
| CLI | `harborrag chat MESSAGE` | One-shot operator requests and scripts |
| MCP | `chat` tool | MCP clients and the local Tool Playground; not yet retrieval-grounded |

Chat does not persist a conversation or ingest its messages — each call is a
single-turn retrieve-then-answer.

## Configure the model

The checked-in runtime catalog uses one logical model named `primary`:

```yaml
chat:
  default_model: primary
  models:
    primary:
      deployments:
        - name: openai-primary
          provider: ${HARBOR_CHAT_PROVIDER}
          model: ${HARBOR_CHAT_MODEL}
          api_key: ${HARBOR_CHAT_API_KEY}
```

Copy the environment template, replace its placeholders, and keep the
populated file out of version control:

```bash
cp env-example/.env.models.example env/.env.models
```

The relevant values are `HARBOR_CHAT_PROVIDER`, `HARBOR_CHAT_MODEL`, and
`HARBOR_CHAT_API_KEY`. `HARBORRAG_MODEL_CONFIG_PATH` selects a different model
catalog. Configuration loading expands environment references but does not
load `.env` files itself; the deployment scripts and Compose services load
`env/.env.models` for you.

Callers select only a system-prompt name; they do not accept provider
credentials, base URLs, custom headers, tools, provider-specific parameters,
or model/sampling overrides. The deployed model, temperature, and token
limits come entirely from `config/models.yaml`.

## Configure retrieval

Two `HARBORRAG_`-prefixed runtime settings control how chat retrieves context
for every HTTP and CLI call:

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_CHAT_RETRIEVAL_TOP_K` | `5` | Number of chunks retrieved as context per call |
| `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` | `false` | When `true`, also runs graph search (FalkorDB traversal) alongside vector search |

**Current limitation:** the retrieval engine only surfaces `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH`'s
graph traversal as diagnostics/telemetry (`RetrievalDiagnostics.graph_nodes` /
`graph_relations`) — it does not yet add graph-discovered content to the
chunks used to ground the answer. Enabling the flag runs the extra graph
query (added latency, no functional effect on the answer's context yet).
Making graph search actually expand the retrieved context is a retrieval-engine
change, not a chat-layer one.

Retrieval always runs hybrid (dense + sparse) vector search; graph search is
strictly additive on top of it. Graph search adds latency, so it defaults to
off.

## Server-owned prompts

The runtime packages two Markdown system prompts, both instructing the model
to answer from the retrieved context and say so when that context is
insufficient:

| Name | Purpose |
| --- | --- |
| `default` | General HarborRAG assistant behavior |
| `concise` | Short, direct answers |

CLI and MCP use `default` unless another prompt is selected.

Prompt names are a controlled public enum; callers cannot provide filesystem
paths or replace the stored catalog. The templates live under
`packages/harborrag-runtime/src/harborrag_runtime/chat/prompts/templates/`.

## HTTP API

Start the development API, then send a completion request:

```bash
scripts/deployment/dev.sh api

curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "tenant": "DEFAULT",
    "system": "concise",
    "prompt": "Explain HarborRAG in one paragraph."
  }' \
  http://127.0.0.1:8000/v1/chat/completions
```

The route requires the `reader` role when API authentication is enabled. Add
`Authorization: Bearer <token>` in that mode. The local development template
uses `HARBORRAG_AUTH_MODE=none` and therefore needs no header.

The request body is `tenant` (defaults to `DEFAULT`), `system` (`default` or
`concise`, defaults to `default`), and `prompt` — a single 1–65,536 character
string that is both the retrieval query and the user message.

A successful response has this stable shape:

```json
{
  "id": "completion-id",
  "model": "primary",
  "provider": "openai",
  "provider_model": "openai/model-name",
  "message": {"role": "assistant", "content": "..."},
  "finish_reason": "stop",
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 18,
    "total_tokens": 60
  },
  "retry_count": 0,
  "fallback_count": 0,
  "citations": [
    {"document_id": "document:...", "chunk_id": "chunk:...", "score": 0.83}
  ]
}
```

`citations` lists the retrieved chunks used as context, ranked by the
retrieval engine, so callers can verify or display sources. It is empty when
retrieval finds nothing relevant.

### Streaming

`POST /v1/chat/stream` accepts the identical request body and streams
Server-Sent Events instead of one JSON object:

```bash
curl --no-buffer --request POST \
  --header 'Content-Type: application/json' \
  --data '{"tenant": "DEFAULT", "system": "concise", "prompt": "Explain HarborRAG in one paragraph."}' \
  http://127.0.0.1:8000/v1/chat/stream
```

The stream emits, in order: one `citations` event, then one or more model
event frames (`text_delta`, `reasoning_delta`, `usage`, `completed`, ...,
mirroring the underlying provider stream), and ends either after `completed`
or with a terminal `error` event. Each frame is `event: <name>\ndata: <json>\n\n`.

The response status is always `200 text/event-stream`: once the stream
starts, HTTP status can no longer change, so failures — including a
prepare-time failure such as an unreachable retrieval or chat backend —
surface as the in-band `error` event rather than a `503`.

## CLI

```bash
uv run --package harborrag-app harborrag chat \
  "Explain HarborRAG in one paragraph." \
  --tenant DEFAULT \
  --system concise
```

Use `--json` for the stable machine-readable command envelope, which includes
the same `citations` field as the HTTP response.

## MCP

The MCP `chat` tool requires `message` and `tenant_id`. Optional arguments are
`system`, `prompt`, `model`, `temperature`, and `max_tokens`. Unlike the HTTP
and CLI surfaces, it calls the chat model directly without retrieval — pair it
with the `vector_search` tool if the answer needs indexed HarborRAG content.

```json
{
  "message": "Explain HarborRAG in one paragraph.",
  "tenant_id": "DEFAULT",
  "prompt": "concise",
  "model": "primary",
  "temperature": 0.2,
  "max_tokens": 300
}
```

Run `scripts/deployment/mcp.sh --check` to verify that the tool is registered.
Run `scripts/deployment/dev.sh bootstrap` first if the protected environment
files do not exist. For browser use, start `scripts/deployment/mcp.sh --http`, open
`http://127.0.0.1:8010/`, authenticate with the token stored in
`env/.env.mcp`, and select `chat` in the Tool Playground.

See [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md)
for stdio clients, HTTP endpoints, tool configuration, and Docker usage.

## Data and error behavior

Every transport marks chat requests as sensitive, disabling model-response
caching unless a separately reviewed model policy explicitly allows it. Raw
prompts and model output are excluded from HarborRAG application logs and MCP
audit records. MCP audits store an argument digest and outcome, not the raw
request or bearer token.

Public transports expose normalized errors. Provider exceptions and secrets
remain server-side. A `503` from `/v1/chat/completions`, an `error` event from
`/v1/chat/stream`, or `chat backend failed` from MCP usually means the model
configuration, credentials, provider reachability, retrieval backend, or
provider context limit must be checked in server logs.
