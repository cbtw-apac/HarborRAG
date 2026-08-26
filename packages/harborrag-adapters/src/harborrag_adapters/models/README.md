# HarborRAG model adapters

The model package exposes independent chat, embedding, and reranking clients. Stable
Harbor request/response/error contracts live in `harborrag_core.models`; LiteLLM
parameter translation, credentials, and provider metadata stay inside this adapter
package.

Install the optional runtime dependencies with `harborrag-adapters[llm]`. The
repository root contains `models.example.yaml` for minimal setup and
`models.advance.examples.yaml` for production-oriented profiles and provider
examples. Copy `.env.llm.example` to an application-controlled environment file or
export its variables; configuration loading expands environment references but does
not implicitly load `.env` files.

## Package layout

Each adapter family exports its public clients and configuration at the family root.
Applications import contracts from
`harborrag_core.models.chat`, `harborrag_core.models.embed`,
`harborrag_core.models.rerank`, and the shared core context, capability, error, and
protocol modules. Provider integrations and LiteLLM translation stay in adapters.
Adapter `errors.py` modules only classify and normalize provider SDK failures into
core exception types.

## Minimal setup

Install the model extra, provide only the credentials referenced by the selected
configuration, and load each independently usable model family from the same file:

```python
from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig

chat_config = HarborChatClientConfig.from_file("models.example.yaml")
embed_config = HarborEmbedClientConfig.from_file("models.example.yaml")
rerank_config = HarborRerankClientConfig.from_file("models.example.yaml")
```

Missing `${ENVIRONMENT_VARIABLE}` references fail during loading. The optional
`${VARIABLE:-default}` form is appropriate for non-secret operational values, not
production credentials. YAML and JSON files, Python mappings, and validated Python
configuration objects use the same Pydantic v2 schemas.

## Advanced setup and precedence

The advanced example provides `production`, `harbor-round-robin`,
`cloud-providers`, and `local` profiles. Select one profile and optionally supply a
small application override:

```python
chat_config = HarborChatClientConfig.from_file(
    "models.advance.examples.yaml",
    profile="production",
    overrides={"timeouts": {"request_seconds": 45}},
)
```

Configuration precedence, from lowest to highest, is:

1. The model-family base section.
2. The selected profile, deep-merged into that section.
3. Explicit `overrides` supplied by the application.
4. Explicit request fields, which take precedence over logical-model defaults.

Nested mappings are merged; lists and scalar values are replaced. Environment and
secret references are resolved after profile and override selection. Configuration
objects remain immutable after validation.

## Logical models and profiles

A profile is a named, partial override of the base configuration. For example,
`profile="local"` selects Ollama chat and embedding models plus an Infinity reranker.
Profiles avoid duplicating complete configuration for each environment.

Logical model names and aliases are stable application identifiers. The model
argument may use either form:

```python
response = client.chat(messages, model="assistant")
```

One logical model may contain multiple interchangeable deployments. Ordered
`fallbacks` reference other logical models and are checked for unknown targets and
cycles while loading.

## Routing and reliability

Routing, retry, fallback, concurrency, and cache policy are shared across model
families. Harbor owns the policy state machine and normalized counters. With
`routing.engine: litellm_router`, chat and embedding use LiteLLM Router for the
selected deployment's transport limits and provider budgets; Harbor still makes each
retry and fallback decision. Reranking uses `routing.engine: harbor` because LiteLLM
Router does not expose a reliable synchronous rerank path.

Retries proceed in explicit stages: retry the selected deployment, switch to another
deployment of the same logical model, then follow the configured logical-model
fallback chain. Non-retryable failures stop immediately. Response metadata reports
each stage separately under `provider_metadata.routing`.

Retryable failures include rate limits, timeouts, connection failures, and selected
provider/server failures. Backoff is exponential, bounded by `max_delay_seconds`, and
randomized by `jitter_ratio`. The circuit breaker supplies deployment cooldown and
health recovery; `max_parallel_requests` provides an optional per-deployment
concurrency limit. Provider budgets are passed to LiteLLM Router for chat and
embedding. Harbor reranking always uses `routing.engine: harbor` because LiteLLM
Router lacks a reliable synchronous rerank path.

`weighted`, `round_robin`, `least_busy`, `latency`, and `ordered` are Harbor selector
strategies. `round_robin` is not accepted with LiteLLM Router. Deployments at the
lowest configured `order` form the current selection group; excluded or unhealthy
deployments allow the next route to be considered.

## Caching

Caching is disabled by default. An eligible request must opt in with `cacheable=True`,
must have a tenant identifier under the default policy, and must not be marked
sensitive unless `cache_sensitive_requests` is explicitly enabled. Cache keys include
a tenant partition and canonical request semantics. Set `cache.backend: custom` for
the built-in bounded TTL cache or inject a cache implementing `ModelResponseCache`;
`litellm` delegates storage while Harbor supplies the deterministic isolated key.
The advanced `production` profile demonstrates enabling chat, embedding, and rerank
caches with TTLs and family-specific namespaces. `cacheable=False` is the
request-level bypass. Requests marked `sensitive=True` remain excluded unless the
configuration explicitly permits sensitive caching.

Redis connection and credential settings are intentionally not accepted by Harbor's
model YAML. When `cache.backend: litellm` is selected, configure LiteLLM's Redis or
hosted cache once at application startup. For `cache.backend: custom`, inject a
`ModelResponseCache`; otherwise Harbor uses its bounded in-memory TTL implementation.

## Observability

Telemetry is an injected boundary shared by chat, embedding, and reranking. Public
clients emit provider-neutral request, stream, retry, fallback, cache, completion,
and error events through `TelemetryDispatcher`; they do not import Langfuse or
OpenTelemetry directly. Structured logging, Langfuse, and OpenTelemetry sinks may be
combined, and exporter failures are isolated by default.
Install the corresponding `langfuse` and `opentelemetry` extras only for the sinks an
application enables.

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

Injected telemetry is borrowed by default. Set
`ChatClientDependencies(telemetry_ownership=ResourceOwnership.OWNED, ...)` when one
client should close and flush it, or close the shared dispatcher at the application
boundary. The privacy defaults
disable prompt and response logging, hash tenant and user identifiers, allowlist
metadata, redact credential-shaped fields, and bound logged content.
`failure_mode: raise` is available only for applications that intentionally require
strict telemetry.

Register the LiteLLM callback once during application startup to capture sanitized
provider-completion and provider-error events. Harbor injects only request correlation
metadata into provider calls; callback registration remains an application-level
choice and does not mutate LiteLLM global state during client construction.

Request metadata supplies correlation and isolation fields at runtime:

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

The safe defaults disable prompt and response logging, hash tenant and user
identifiers, redact credential-shaped fields, enforce a metadata allowlist, and bound
logged content. Content logging must be deliberately enabled in each family.

## Provider support and limitations

| Provider | Chat | Embedding | Reranking | Notes |
| --- | --- | --- | --- | --- |
| OpenAI | Yes | Yes | No | Default hosted examples. |
| Azure OpenAI | Yes | Yes | No | Requires API base, version, and deployment name. |
| Anthropic | Yes | No | No | Chat-only registry entry. |
| AWS Bedrock | Yes | Yes | Yes | Region required; ambient or complete static credentials. |
| Gemini | Yes | Yes | No | API-key examples; Vertex AI is a separate embedding provider. |
| Ollama | Yes | Yes | No | Local HTTP endpoint; capabilities are model-specific. |
| OpenAI-compatible | Yes | Yes | No | Custom base required; declare capabilities conservatively. |
| Cohere | No | Yes | Yes | The base advanced file uses Cohere reranking. |
| Infinity | No | Yes | Yes | The local profile demonstrates reranking. |

These are adapter boundaries, not guarantees that every provider model supports every
optional feature. Model identifiers, cloud entitlements, regions, and capability
flags must match the actual deployment. Reranking is never emulated through chat.

## Chat

```python
from harborrag_adapters.models.chat import ChatClientFactory, HarborChatClientConfig

config = HarborChatClientConfig.from_file("models.example.yaml")
with ChatClientFactory.create(config) as client:
    response = client.chat([{"role": "user", "content": "Summarize HarborRAG."}])
    print(response.text)
```

Use `ChatClientFactory.create_async(config)` for asynchronous calls with
`await client.achat(...)`. The synchronous client exposes `stream(...)`; the
asynchronous client exposes `astream(...)`. Both return normalized text, tool-call,
usage, and completion events. Tool definitions use the core `HarborChatTool` schema;
complete responses and completion events expose parsed tool calls. Applications own
tool execution and submit any results as tool messages in a later request.

Use `client.chat_structured(..., response_model=MyPydanticModel)` or its asynchronous
counterpart for validated structured data. Each deployment explicitly declares
native-schema, JSON-mode, and multimodal capabilities. The structured-output policy
may allow JSON degradation or opt into prompt fallback, and repair attempts are
strictly bounded. Image and text parts use the provider-neutral core content schemas.

Chat exposes distinct synchronous and asynchronous clients through one narrow
factory. Each logical model may contain multiple enabled deployments and ordered
fallback references.

## Embeddings

```python
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig

config = HarborEmbedClientConfig.from_file("models.example.yaml")
with HarborEmbedClient.from_config(config) as client:
    response = client.embed(["first document", "second document"])
    vectors = response.vectors
```

Every automatic batch in one attempt is pinned to one deployment, usage is
aggregated across batches, and output order matches input order. A terminal failure
after an earlier batch raises `HarborEmbedPartialBatchError`; partial vectors are not
returned or replayed. A failure before any batch completes may use the shared retry,
deployment-failover, and logical-model-fallback policy.

## Reranking

```python
from harborrag_adapters.models.rerank import HarborRerankClientConfig, HarborRerankingClient

config = HarborRerankClientConfig.from_file("models.example.yaml")
with HarborRerankingClient.from_config(config) as client:
    response = client.rerank("harbor search", ["candidate one", "candidate two"])
    print(response.results)
```

The complete candidate set is sent to one deployment. Results retain source document
indexes and metadata and are sorted deterministically by descending score, then source
index for ties. Multiple deployments, logical-model fallbacks, shared retries, and
tenant-isolated response caching use the same policy layer as chat and embedding.

## Extending configuration safely

- Add a deployment to an existing logical model only when its behavior and output
  space are interchangeable; otherwise create a new logical model.
- Keep embedding fallbacks in the same declared `embedding_space` and dimension.
- Add providers and custom endpoint hosts to the security allowlists deliberately.
- Declare only capabilities verified for the concrete provider model. Capability
  flags control validation for streaming, tools, structured output, JSON mode, and
  multimodal requests.
- Keep credentials in environment or secret references. Do not place plaintext
  secrets in YAML, headers, or provider extension parameters.
- Use `extra_litellm_params` only for fields listed by the family security policy.
  Custom providers additionally require `allow_custom_providers: true` and an
  explicit `custom_llm_provider`.
- Load every changed profile through all affected configuration classes in tests so
  unsupported and ignored fields fail immediately.
