# Chat

HarborRAG exposes the same provider-neutral chat runtime through HTTP, the
CLI, and MCP. All three surfaces load the `chat` family from
`config/models.yaml` and call `AsyncHarborChatClient` through the runtime
facade.

| Surface | Entry point | Best for |
| --- | --- | --- |
| HTTP API | `POST /v1/chat/completions` | Applications and authenticated services |
| CLI | `harborrag chat MESSAGE` | One-shot operator requests and scripts |
| MCP | `chat` tool | MCP clients and the local Tool Playground |

Chat is currently non-streaming and stateless. A call does not run retrieval,
persist a conversation, or ingest its messages. Retrieve evidence separately
when the answer needs indexed HarborRAG content.

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

Callers select only logical model names such as `primary`. HTTP, CLI, and MCP
do not accept provider credentials, base URLs, custom headers, tools, or
provider-specific parameters.

## Server-owned prompts

The runtime packages two Markdown system prompts:

| Name | Purpose |
| --- | --- |
| `default` | General HarborRAG assistant behavior |
| `concise` | Short, direct answers |

CLI and MCP use `default` unless another prompt is selected. The HTTP API
prepends no stored prompt when `prompt` is omitted. A selected stored prompt is
inserted before any system message supplied by the caller.

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
    "model": "primary",
    "prompt": "concise",
    "messages": [
      {"role": "user", "content": "Explain HarborRAG in one paragraph."}
    ],
    "temperature": 0.2,
    "max_tokens": 300
  }' \
  http://127.0.0.1:8000/v1/chat/completions
```

The route requires the `reader` role when API authentication is enabled. Add
`Authorization: Bearer <token>` in that mode. The local development template
uses `HARBORRAG_AUTH_MODE=none` and therefore needs no header.

The request accepts 1–100 messages with roles `system`, `developer`, `user`,
or `assistant`. It also accepts `top_p`, up to four stop sequences, and a
`seed`. The combined message content is limited to 131,072 characters and
`max_tokens` is limited to 32,768. Provider context-window limits can be lower;
character validation is not a token-count guarantee.

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
  "fallback_count": 0
}
```

## CLI

```bash
uv run --package harborrag-app harborrag chat \
  "Explain HarborRAG in one paragraph." \
  --tenant DEFAULT \
  --prompt concise \
  --model primary \
  --temperature 0.2 \
  --max-tokens 300
```

Use `--system TEXT` for a request-specific system message and `--json` for the
stable machine-readable command envelope.

## MCP

The MCP `chat` tool requires `message` and `tenant_id`. Optional arguments are
`system`, `prompt`, `model`, `temperature`, and `max_tokens`.

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
remain server-side. A `503` from the HTTP API or `chat backend failed` from MCP
usually means the model configuration, credentials, provider reachability, or
provider context limit must be checked in server logs.
