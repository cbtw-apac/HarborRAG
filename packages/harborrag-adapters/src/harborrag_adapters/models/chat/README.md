# Chat model adapter

## Responsibility

Turns the provider-neutral chat ports in `harborrag-core` into normalized
LiteLLM-backed completions. This capability owns chat configuration validation,
provider invocation, retry and routing policy execution, structured output,
stream decoding, batching, and resource lifecycle.

## Inputs / Outputs

```text
HarborChatClientConfig + HarborChatRequest
    -> HarborChatResponse | HarborChatStreamChunk | validated Pydantic model
```

`HarborChatClient` implements the synchronous core port.
`AsyncHarborChatClient` implements the asynchronous core port. They share internal
execution and policy components, but neither exposes the other client's completion
methods.

## Must not

- define domain chat schemas or ports; those belong in `harborrag-core`
- select a chat model for an engine workflow; that decision belongs in engine policy
- expose LiteLLM response objects to consumers
- import API, CLI, MCP, engine, or Temporal packages
- register Langfuse or OpenTelemetry globals during client construction
- add a provider plugin registry while LiteLLM remains the only provider path

## Public entry point

Use `ChatClientFactory` to turn validated configuration into a ready client:

```python
from harborrag_adapters.models.chat import (
    ChatClientFactory,
    HarborChatClientConfig,
)

config = HarborChatClientConfig.from_file("config/models.example.yaml")

with ChatClientFactory.create(config) as client:
    response = client.chat([{"role": "user", "content": "Summarize HarborRAG."}])
```

Asynchronous applications select the async boundary explicitly:

```python
async with ChatClientFactory.create_async(config) as client:
    response = await client.achat(
        [{"role": "user", "content": "Summarize HarborRAG."}]
    )
```

Tests and application composition roots may inject owned or borrowed dependencies
without expanding the client constructor:

```python
from harborrag_adapters.models.chat import (
    ChatClientDependencies,
    ChatClientFactory,
)

client = ChatClientFactory.create(
    config,
    ChatClientDependencies(telemetry=telemetry),
)
```

## Contract / Policy / Implementation

- Contract: `HarborChatClientProtocol` and `AsyncHarborChatClientProtocol` live in
  `harborrag-core`.
- Policy: routing, retry, structured-output selection, and request preparation stay
  separate from transport invocation.
- Implementation: the public clients compose execution, streaming, lifecycle, and
  LiteLLM backend components through `client_runtime.py`.

The Factory pattern is used because consumers need a ready sync or async instance
from configuration. No client plugin Registry is used: LiteLLM is currently the
single provider path, while backend selection is an internal transport concern.

## Failure modes

Configuration and capability errors fail before provider I/O. Provider,
authentication, rate-limit, timeout, connection, malformed-response, streaming,
and structured-output failures are normalized to core chat exceptions. Structured
repair attempts and retry/fallback attempts are bounded by configuration.

## Tests

`tests/models/contract/chat/chat_client_contract.py` defines one port contract suite.
`test_chat_clients.py` runs that suite against both public clients. Backend contract
tests cover the direct SDK, LiteLLM Router, and LiteLLM Proxy transports separately.
Real credentialed provider checks remain in the marked smoke suite.
