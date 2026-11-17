# Chat model adapter

This package turns the provider-neutral chat ports of `harborrag-core` into
LiteLLM-backed completions with normalized results. It owns chat configuration
validation, provider calls, retry and routing, structured output, stream decoding,
batching and the client lifecycle. Read it before you change how HarborRAG calls a chat
model; the configuration, routing and caching it shares with embedding and reranking
are described in [Model adapters](../README.md).

## Inputs and outputs

```text
HarborChatClientConfig + HarborChatRequest
    -> HarborChatResponse | HarborChatStreamChunk | validated Pydantic model
```

`HarborChatClient` implements the synchronous core port, `HarborChatClientProtocol`.
`AsyncHarborChatClient` implements the asynchronous one,
`AsyncHarborChatClientProtocol`. Both protocols live in
`harborrag_core.ports.model_clients`. The two clients share internal execution and
policy components, but neither exposes the other's completion methods.

## Create a client

`ChatClientFactory` turns a validated configuration into a ready client:

```python
from harborrag_adapters.models.chat import ChatClientFactory, HarborChatClientConfig

config = HarborChatClientConfig.from_file("config/models/models.yaml")

with ChatClientFactory.create(config) as client:
    response = client.chat([{"role": "user", "content": "Summarize HarborRAG."}])
```

An asynchronous application asks for the asynchronous client:

```python
async with ChatClientFactory.create_async(config) as client:
    response = await client.achat([{"role": "user", "content": "Summarize HarborRAG."}])
```

Tests and composition roots inject owned or borrowed dependencies, such as telemetry,
without widening the client constructor:

```python
from harborrag_adapters.models.chat import ChatClientDependencies, ChatClientFactory

client = ChatClientFactory.create(
    config,
    ChatClientDependencies(telemetry=telemetry),
)
```

## How the package is built

- **Contract:** the two client protocols in `harborrag-core`.
- **Policy:** routing, retry, structured-output selection and request preparation,
  kept apart from transport calls.
- **Implementation:** the public clients compose execution, streaming, lifecycle and
  the LiteLLM backend through `client_runtime.py`.

`backend.type` picks the LiteLLM transport: `direct_sdk` (the default),
`litellm_router` or `litellm_proxy` (`ChatBackendType` in `backend_config.py`).

The package uses a factory because callers need a ready synchronous or asynchronous
client from configuration. It has no client plugin registry: LiteLLM is the only
provider path, and the backend choice is an internal transport detail. The rule for
adding a second path is in
[CONTRIBUTING: developer map](../../../../../../CONTRIBUTING.md#developer-map).

## What this package must not do

- Define domain chat schemas or ports; those belong in `harborrag-core`.
- Choose the chat model for an engine workflow; that is engine policy.
- Expose LiteLLM response objects to callers.
- Import the API, CLI, MCP, engine or Temporal packages.
- Register Langfuse or OpenTelemetry globals while building a client.
- Add a provider plugin registry while LiteLLM is the only provider path.

## Failure modes

Configuration and capability errors fail before any provider call. Provider,
authentication, rate-limit, timeout, connection, malformed-response, streaming and
structured-output failures are normalized to core chat exceptions. Structured repair
attempts and retry and fallback attempts are bounded by configuration.

For structured extraction, the optional `timeouts.operation_seconds` bounds the whole
`achat_structured` operation, including admission, routing retries and output repairs,
and cancelling it releases the deployment's admission slot. The synchronous
`chat_structured` passes the remaining time to each provider call and checks the
deadline between calls, so a blocking custom transport must honour its own timeouts.
The deadline is unset by default and does not change ordinary chat or streaming.

## Reasoning options

Each deployment can set these:

- `reasoning.enable_thinking: false` sends
  `extra_body.chat_template_kwargs.enable_thinking`. It is an explicit opt-in for an
  endpoint or template, does not widen the extension allowlist, and is rejected when it
  conflicts with another body extension.
- `reasoning.parse_think_tags: true` separates one complete leading
  `<think>...</think>` block from a non-streamed response and rejects an unterminated
  one. Tags inside the answer text stay as they are.
- `capabilities.reasoning_effort` declares that the deployment supports the request's
  effort parameter. `reasoning_content` describes response content instead.

## Caching

Chat cache keys include a masked fingerprint of the configuration, as well as the
request semantics and the tenant, so changing a deployment or template setting
invalidates shared entries. Caching stays off by default; extraction callers can keep
using `cacheable=False` and durable extraction artifacts.

## Tests

- `packages/harborrag-adapters/tests/models/contract/chat/chat_client_contract.py`
  defines one port contract suite, and `test_chat_clients.py` runs it against both
  public clients.
- `test_chat_backends.py` in the same folder checks the direct SDK, LiteLLM Router and
  LiteLLM Proxy transports separately.
- Checks against real, credentialed providers are in the marked smoke suite under
  `packages/harborrag-adapters/tests/models/smoke/`.
