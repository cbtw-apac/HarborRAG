# HarborRAG model adapters

This package holds HarborRAG's three model clients: chat, which writes answers and
extracts graph entities; embedding, which turns text into vectors; and reranking,
which reorders search results. This page is for contributors: how the package is laid
out, how a client loads its configuration, and the routing, caching and telemetry
behaviour the clients share.

The request, response and error contracts live in `harborrag_core.models`. LiteLLM
parameter translation, credentials and provider metadata stay in this package. The
model catalog itself is described for users in
[Model configuration](../../../../../docs/users/configuration/model-config.md).

## Package layout

| Folder | What it holds |
| --- | --- |
| `chat/` | `HarborChatClient`, `AsyncHarborChatClient`, `ChatClientFactory` and `HarborChatClientConfig`; see [Chat model adapter](chat/README.md) |
| `embed/` | `HarborEmbedClient` and `HarborEmbedClientConfig` |
| `rerank/` | `HarborRerankingClient` and `HarborRerankClientConfig` |
| `runtime/` | Policy the three families share: configuration loading, routing, retry, cache, budgets, health, telemetry and security |

Each family exports its public clients and configuration from the family folder.
Applications import contracts from `harborrag_core.models.chat`,
`harborrag_core.models.embed`, `harborrag_core.models.rerank` and the shared core
context, capability, error and protocol modules. A family's `errors.py` only classifies
provider SDK failures and normalizes them into core exception types.

`python -m harborrag_adapters.models` checks a catalog without calling a provider; see
[Check a catalog before you use it](../../../../../docs/users/configuration/model-config.md#check-a-catalog-before-you-use-it).

## Load a configuration

The clients need the `llm` extra (`harborrag-adapters[llm]`). The repository's
`config/models/models.yaml` defines all three families, each behind one LiteLLM proxy
gateway, and `config/models/models.production.example.yaml` calls providers directly
with fallbacks and profiles.

```python
from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig

chat_config = HarborChatClientConfig.from_file("config/models/models.yaml")
embed_config = HarborEmbedClientConfig.from_file("config/models/models.yaml")
rerank_config = HarborRerankClientConfig.from_file("config/models/models.yaml")
```

Each family loads only its own section, so loading `chat` from Python needs only the
variables the `chat` section references. A variable that a loaded section references
and that is not set fails the load, for example
`environment variable HARBORRAG_RERANKER_MODEL is not set`. The runtime builds the
rerank client whenever the catalog has a `rerank` section
(`harborrag_runtime/retrieval/composition.py`), so an application running the shipped
catalog still needs all five gateway variables. YAML and JSON files,
Python mappings and validated configuration objects all go through the same Pydantic
schemas, and a configuration object is immutable once validated.

`${NAME}` references, `.env` handling and profile precedence are described in
[Model configuration: environment and secret references](../../../../../docs/users/configuration/model-config.md#environment-and-secret-references)
and
[profiles and precedence](../../../../../docs/users/configuration/model-config.md#profiles-and-precedence).
`from_file` takes the profile and the code overrides as keyword arguments:

```python
chat_config = HarborChatClientConfig.from_file(
    "config/models/models.production.example.yaml",
    profile="production",
    overrides={"timeouts": {"request_seconds": 45}},
)
```

Environment and secret references are resolved after the profile and the overrides
are applied.

## Logical models and deployments

A logical model, such as `primary`, is the stable name the application asks for; it can
also have aliases. The `model` argument accepts either:

```python
response = client.chat(messages, model="primary")
```

A logical model holds one or more interchangeable deployments. Its ordered `fallbacks`
name other logical models; loading fails on an unknown fallback target or a fallback
cycle.

## Routing and reliability

Harbor owns the policy state machine and the normalized counters for routing, retry,
fallback, concurrency and cache in all three families. Retries run in stages:

1. Retry the selected deployment.
2. Switch to another deployment of the same logical model.
3. Follow the logical model's fallback chain.

A non-retryable failure stops at once. Response metadata reports each stage under
`provider_metadata.routing`.

Retryable failures are rate limits, timeouts, connection failures and selected provider
or server failures. Backoff is exponential, capped by `max_delay_seconds` and randomized
by `jitter_ratio`. The circuit breaker cools a failing deployment down and recovers it,
and `max_parallel_requests` optionally caps one deployment's concurrent requests.

`routing.engine` is `harbor` or `litellm_router`:

- With `litellm_router`, chat and embedding use LiteLLM Router for the selected
  deployment's transport limits and provider budgets; Harbor still makes every retry and
  fallback decision. `round_robin` is rejected with this engine.
- Reranking accepts only `harbor`, because LiteLLM Router has no reliable synchronous
  rerank path.

The selector strategies are `ordered`, `weighted`, `round_robin`, `least_busy` and
`latency`. Deployments at the lowest configured `order` form the current selection
group; an excluded or unhealthy deployment lets the next one be considered.

## Caching

Caching is off by default. A request is cached only when it opts in with
`cacheable=True`, carries a tenant identifier (under the default policy), and is not
marked `sensitive=True`, unless `cache_sensitive_requests` is on. Cache keys include a
tenant partition and the canonical request semantics.

| `cache.backend` | Storage |
| --- | --- |
| `custom` (default) | The built-in bounded in-memory TTL cache, or a `ModelResponseCache` you inject |
| `redis` | Harbor's own Redis cache, configured by the `redis:` block of the same file |
| `litellm` | LiteLLM's cache, which owns storage while Harbor supplies the isolated key; configure it once at application startup |

The `redis:` block (`url`, `key_prefix`, `max_connections` and socket timeouts) is
required whenever a Redis cache, Redis routing state or Redis singleflight is selected.

## Observability

Telemetry is an injected boundary shared by the three families. The clients emit
provider-neutral request, stream, retry, fallback, cache, completion and error events
through a `TelemetryDispatcher`, and never import Langfuse or OpenTelemetry themselves.
Structured logging, Langfuse and OpenTelemetry sinks can be combined; install the
`langfuse` or `opentelemetry` extra for the sinks you use.

```python
import logging

import litellm

from harborrag_adapters.models.chat import ChatClientDependencies, ChatClientFactory
from harborrag_adapters.models.runtime import (
    LangfuseTelemetry,
    LiteLLMTelemetryCallback,
    OpenTelemetryTelemetry,
    StructuredLoggingTelemetry,
    TelemetryDispatcher,
)

telemetry = TelemetryDispatcher(
    [
        StructuredLoggingTelemetry(logging.getLogger("harborrag.models")),
        LangfuseTelemetry(),
        OpenTelemetryTelemetry(),
    ],
    config=chat_config.observability,
)
client = ChatClientFactory.create(
    chat_config,
    ChatClientDependencies(telemetry=telemetry),
)
litellm.callbacks.append(LiteLLMTelemetryCallback(telemetry))
```

`LangfuseTelemetry()` reads its keys from the Langfuse SDK's own variables, such as
`LANGFUSE_PUBLIC_KEY`; without them the Langfuse client logs that it is disabled.

- Injected telemetry is borrowed. Pass
  `ChatClientDependencies(telemetry_ownership=ResourceOwnership.OWNED, ...)` when one
  client should flush and close it, or close the shared dispatcher at the application
  boundary.
- Register `LiteLLMTelemetryCallback` once at startup to capture sanitized provider
  completion and error events. Client construction never changes LiteLLM's global
  state.
- Exporter failures are isolated by default; `failure_mode: raise` is for applications
  that want strict telemetry.
- The privacy defaults turn off prompt and response logging, hash tenant and user
  identifiers, keep only allowlisted metadata, redact credential-shaped fields and
  bound logged content. Content logging must be switched on in each family.

Request metadata carries the correlation and isolation fields:

```python
response = client.chat(
    messages,
    metadata={
        "request_id": "request-123",
        "trace_id": "trace-123",
        "workflow_id": "ingestion-42",
        "tenant_id": "tenant-7",
        "user_id": "user-9",
    },
)
```

## Providers

The `provider` value of a deployment must be one of its family's provider enum, which
is the source of truth for this table:

| Family | Enum | Providers |
| --- | --- | --- |
| Chat | `HarborProvider` in `chat/registry.py` | `openai`, `azure_openai`, `bedrock`, `anthropic`, `gemini`, `ollama`, `openai_compatible`, `vllm`, `litellm_proxy`, `custom` |
| Embedding | `HarborEmbedProvider` in `embed/registry.py` | `openai`, `azure_openai`, `bedrock`, `cohere`, `gemini`, `vertex_ai`, `voyage`, `huggingface`, `ollama`, `openai_compatible`, `infinity`, `vllm`, `litellm_proxy`, `custom` |
| Reranking | `HarborRerankProvider` in `rerank/registry.py` | `cohere`, `bedrock`, `together_ai`, `azure_ai`, `jina_ai`, `huggingface`, `infinity`, `vllm`, `nvidia_nim`, `deepinfra`, `vertex_ai`, `fireworks_ai`, `voyage`, `watsonx`, `litellm_proxy`, `custom` |

Each provider's `ProviderMetadata` in the same registry declares the fields a
deployment must set (`required_fields`) and which credentials it accepts. For example,
`azure_openai` needs `api_base`, `api_version` and `deployment_name`; `bedrock` needs
`aws_region_name` and uses either ambient AWS credentials or both
`aws_access_key_id` and `aws_secret_access_key`; `openai_compatible` and several
self-hosted providers need `api_base`; and `custom` needs `custom_llm_provider`.

A provider being listed does not mean every model it serves supports every feature.
Model identifiers, cloud entitlements, regions and capability flags must match the real
deployment, and reranking is never emulated through chat.

## Chat

The smallest chat call is in
[Model configuration: load a family from Python](../../../../../docs/users/configuration/model-config.md#load-a-family-from-python).

- `ChatClientFactory.create(config)` returns the synchronous client, with `chat(...)`
  and `stream(...)`. `ChatClientFactory.create_async(config)` returns the asynchronous
  one, with `achat(...)` and `astream(...)`.
- Streams yield normalized text, tool-call, usage and completion events.
- Tool definitions use the core `HarborChatTool` schema, and responses expose parsed
  tool calls. The application runs the tools and sends their results as tool messages
  in a later request.
- `chat_structured(..., response_model=MyPydanticModel)` and its asynchronous
  counterpart return validated structured data. Each deployment declares its
  native-schema, JSON-mode and multimodal capabilities; the structured-output policy
  can allow JSON degradation or a prompt fallback, and repair attempts are bounded.

Internals, failure modes and reasoning options are in [Chat model adapter](chat/README.md).

## Embeddings

```python
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig

config = HarborEmbedClientConfig.from_file("config/models/models.yaml")
with HarborEmbedClient.from_config(config) as client:
    response = client.embed(["first document", "second document"])
    vectors = response.vectors
```

Every automatic batch in one attempt goes to the same deployment, usage is summed
across batches, and output order matches input order. A terminal failure after an
earlier batch succeeded raises `HarborEmbedPartialBatchError`; partial vectors are not
returned or replayed. A failure before any batch completes can use the shared retry,
deployment failover and fallback policy.

## Reranking

```python
from harborrag_adapters.models.rerank import HarborRerankClientConfig, HarborRerankingClient

config = HarborRerankClientConfig.from_file("config/models/models.yaml")
with HarborRerankingClient.from_config(config) as client:
    response = client.rerank("harbor search", ["candidate one", "candidate two"])
    print(response.results)
```

The whole candidate set goes to one deployment. Results keep each candidate's source
index and metadata, sorted by descending score and then by source index. Multiple
deployments, fallbacks, shared retries and tenant-isolated caching use the same policy
layer as chat and embedding.

## Change a catalog safely

The general rules, such as declaring only verified capabilities and keeping embedding
fallbacks in one embedding space, are in
[Model configuration: reliability and safety controls](../../../../../docs/users/configuration/model-config.md#reliability-and-safety-controls).
In addition:

- Add a deployment to an existing logical model only when its behaviour and output are
  interchangeable with the others; otherwise create a new logical model.
- Capability flags drive validation for streaming, tools, structured output, JSON mode
  and multimodal requests.
- `extra_litellm_params` accepts only the fields the family's security policy lists. A
  custom provider also needs `allow_custom_providers: true` and an explicit
  `custom_llm_provider`.
- In tests, load every changed profile through every configuration class it affects,
  so unsupported or ignored fields fail at once.
