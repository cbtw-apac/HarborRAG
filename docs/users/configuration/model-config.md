# Model Configuration

The model adapter provides separate chat, embedding, and reranking clients. Each family loads its own section from the same YAML or JSON file and returns an immutable Pydantic configuration object.

Start with:

- [`config/models.example.yaml`](../../../config/models.example.yaml) for a compact direct-provider example;
- [`config/models.advance.example.yaml`](../../../config/models.advance.example.yaml) for more routing and provider controls;
- [`config/advance_chat/`](../../../config/advance_chat/) for direct SDK, LiteLLM Router, proxy, and distributed examples.

Install `harborrag-adapters[llm]` when it is not already present in the development environment.

## Minimal structure

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

config = HarborChatClientConfig.from_file("config/models.example.yaml")
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

The repository's runnable `config/models.yaml` uses
`HARBOR_CHAT_PROVIDER`, `HARBOR_CHAT_MODEL`, and `HARBOR_CHAT_API_KEY` for its `primary`
chat deployment, **and** `HARBOR_EMBED_PROVIDER`, `HARBOR_EMBED_MODEL`, and
`HARBOR_EMBED_API_KEY` for its embedding deployment. All six are required: references
expand eagerly, so filling in only the chat three fails at load time. The more general
example files above use provider variables such as `OPENAI_API_KEY` instead.

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
OPENAI_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models validate config/models.example.yaml --family chat

OPENAI_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models explain config/models.example.yaml --family embed

COHERE_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models render config/models.example.yaml \
    --family rerank --format yaml
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
