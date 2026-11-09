# Model Configuration

Configure the chat, embedding and reranking models HarborRAG calls, and check that a
model catalog resolves before anything calls a provider. The model adapter provides a
separate client for each of the three families. Each family loads its own section from
the same YAML or JSON file and returns an immutable Pydantic configuration object.

The `config/models/` folder holds:

- [`config/models/models.yaml`](../../../config/models/models.yaml), the active catalog,
  which sends chat, embedding and reranking through one LiteLLM proxy gateway;
- [`config/models/models.production.example.yaml`](../../../config/models/models.production.example.yaml),
  a catalog that calls providers directly, with routing, fallbacks, reranking and profiles.

Install `harborrag-adapters[llm]` when it is not already present in the development environment.

## Minimal structure

A catalog that calls one provider directly needs no more than this:

```yaml
chat:
  default_model: primary
  backend:
    type: direct_sdk
  security:
    allowed_providers: [openai]
  models:
    primary:
      deployments:
        - name: openai-primary
          provider: openai
          model: openai/REPLACE_WITH_CHAT_MODEL
          api_key: ${OPENAI_API_KEY}
          capabilities:
            streaming: true
            structured_output: true
            json_mode: true
            tools: true
```

Logical model names such as `primary` are stable application identifiers. A logical model may have aliases, multiple deployments, and ordered fallbacks. Each deployment declares its provider, provider model identifier, credentials, and verified capabilities.

## Load a family

```python
from harborrag_adapters.models.chat import ChatClientFactory, HarborChatClientConfig

config = HarborChatClientConfig.from_file("config/models/models.yaml")
with ChatClientFactory.create(config) as client:
    response = client.chat([{"role": "user", "content": "Summarize HarborRAG."}])
    print(response.text)
```

Embedding and reranking use `HarborEmbedClientConfig`/`HarborEmbedClient` and `HarborRerankClientConfig`/`HarborRerankingClient` respectively.

## Application chat composition

The HTTP API, CLI, and MCP server do not construct this adapter directly. The
runtime lazily loads the `chat` family from `HARBORRAG_MODEL_CONFIG_PATH`, owns
one asynchronous client lifecycle, and exposes it through `HarborRAG.chat`.
Public callers may select a logical model but cannot provide a deployment,
credential, base URL, header, tool definition, or provider-specific parameter.

Stored prompts are intentionally separate from model configuration. The
runtime's typed prompt catalog packages the `default` and `concise` Markdown
templates; see [Chat](../chat/README.md) for transport behavior and prompt
selection.

## The shipped gateway catalog

The repository's runnable `config/models/models.yaml` defines one logical model named
`primary` in each family, and every one of them calls the same LiteLLM proxy gateway:

- **Chat** uses the `direct_sdk` backend with one `provider: openai_compatible`
  deployment at the gateway's URL and key, so the LiteLLM SDK calls the gateway as an
  OpenAI-compatible endpoint. Topology extraction accepts only the `direct_sdk`
  backend, which is why chat does not use the `litellm_proxy` backend.
  `chat.security.allowed_providers` is `[openai, openai_compatible]`. The deployment
  does not declare `reasoning_effort`: given a reasoning effort, a Claude model behind
  the gateway answers in prose instead of the requested JSON schema, and topology
  extraction and graph summaries need schema-constrained answers.
- **Embedding** names `provider: cohere` with `custom_llm_provider: litellm_proxy` and
  the gateway's URL and key. `provider: cohere` is what makes HarborRAG send Cohere's
  `input_type`, `search_query` for questions and `search_document` for chunks, and the
  gateway passes it on. The model writes 1536-dimension vectors in the embedding space
  `harbor-cohere-embed-v4`.
- **Reranking** names `provider: litellm_proxy` with the gateway's URL and key. Chat
  retrieval uses it when `retrieval.rerank` is on; see
  [Runtime settings](runtime-settings.md#rerank-chat-retrieval).

The catalog reads five variables from `env/.env.models`: `HARBORRAG_URL`,
`HARBORRAG_API_KEY`, `HARBORRAG_CHAT_MODEL`, `HARBORRAG_EMBED_MODEL` and
`HARBORRAG_RERANKER_MODEL`. [Model gateway environment](config-file-reference.md#model-gateway-environment)
describes each one. All five are required: references expand eagerly, so leaving out
the reranker model fails at load time even if you never switch reranking on.

Do not confuse `HARBORRAG_EMBED_MODEL` with `HARBORRAG_EMBEDDING_MODEL`. The first is
the gateway's name for the embedding model. The second, which you rarely need, selects
a logical model of the catalog's `embed` section, such as `primary`.

Changing the embedding model needs a re-index. Vectors written by the previous model
have the same 1536 dimensions, so nothing reports an error: retrieval quality drops
without warning until every source is re-ingested with `--force-reprocess`.

The production example uses provider variables such as `AZURE_OPENAI_API_KEY` instead.
`env-example/.env.models.example` keeps commented examples for a catalog that calls a
provider or another proxy directly; they stay commented because they set
`HARBORRAG_CHAT_MODEL` and `HARBORRAG_EMBED_MODEL` too.

### Optional memory model

The conversation-memory layer (query rewriting, fact extraction and session summaries)
can use a cheaper model than chat. Add a `memory` logical model under `chat.models` in
`config/models/models.yaml` and set `HARBORRAG_MEMORY_MODEL` in `env/.env.models` to
the gateway's name for that model:

```yaml
    memory:
      deployments:
        - name: gateway-memory
          provider: openai_compatible
          model: ${HARBORRAG_MEMORY_MODEL}
          api_base: ${HARBORRAG_URL}
          api_key: ${HARBORRAG_API_KEY}
          capabilities:
            streaming: false
            structured_output: true
            json_mode: true
            tools: false
```

The memory model calls the same gateway as `primary`, with the same URL and key.
`HARBORRAG_MEMORY_PROVIDER` and `HARBORRAG_MEMORY_API_KEY` belong to a catalog that
calls a provider directly.

Extraction and summarization need schema-constrained answers, so the deployment must
declare `structured_output` and `json_mode`. `HARBORRAG_MEMORY_MODEL_PROFILE` names the
logical model the layer uses (`memory` by default); while no such model exists, the layer
falls back to the default chat model.

## Environment and secret references

`${NAME}` is required. `${NAME:-default}` supplies a default and should be reserved for non-secret operational values. References are expanded while loading, so validation fails before any provider call when a required variable is missing.

Configuration loaders do not read `.env` automatically.
`env-example/.env.models.example` is a template for shell, application,
container, or secret-manager setup.

Validated configuration can also be loaded from a Python mapping with `from_dict(...)`. Secret-manager integration can provide a `SecretResolver` rather than storing plaintext values.

## Profiles and precedence

When a document has a top-level `profiles` mapping, pass `profile="name"` to `from_file`. A selected family configuration is layered as:

```text
family base < selected profile < code overrides < request fields
```

Nested mappings merge recursively; lists and scalar values replace the lower-precedence value.

## Validate and inspect

```bash
uv run --env-file env-example/.env.models.example \
  python -m harborrag_adapters.models validate config/models/models.yaml --family chat

uv run --env-file env-example/.env.models.example \
  python -m harborrag_adapters.models validate config/models/models.yaml --family rerank

uv run --env-file env-example/.env.models.example \
  python -m harborrag_adapters.models explain config/models/models.yaml --family embed

AWS_REGION=us-east-1 COHERE_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models render \
    config/models/models.production.example.yaml --family rerank --format yaml
```

`render` prints a sanitized representation and supports `--output`. These commands validate structure and resolution; they do not prove that placeholder credentials or model identifiers can reach a provider.

## Reliability and safety controls

The shared model configuration supports timeouts, staged retry/failover, routing, circuit breakers, per-deployment concurrency, caching, singleflight, budgets, health state, connection pools, observability, and provider/base-URL allowlists. Chat additionally selects `direct_sdk`, `litellm_router`, or `litellm_proxy` transport behavior.

Important rules:

- Declare only capabilities verified for the concrete provider model.
- Keep embedding fallbacks in the same embedding space and dimension.
- Opt in deliberately to custom providers, endpoint hosts, request auth headers, sensitive caching, and prompt/response logging.
- Keep tenant identifiers in request metadata when tenant-isolated cache, budget, or routing features require them.
- Do not put credentials in committed YAML, headers, fixtures, or rendered output.

For paid, credentialed checks, use the model smoke scripts documented in [Testing](../../developers/testing/README.md#real-system-smoke-checks).
