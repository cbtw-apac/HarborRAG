# Model Configuration

The model adapter provides separate chat, embedding, and reranking clients. Each family loads its own section from the same YAML or JSON file and returns an immutable Pydantic configuration object.

The `config/models/` folder holds:

- [`config/models/models.yaml`](../../../config/models/models.yaml), the active chat and embedding catalog;
- [`config/models/models.production.example.yaml`](../../../config/models/models.production.example.yaml), with routing, fallbacks, reranking and profiles.

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

The repository's runnable `config/models/models.yaml` uses
`HARBORRAG_CHAT_PROVIDER`, `HARBORRAG_CHAT_MODEL`, and `HARBORRAG_CHAT_API_KEY` for its `primary`
chat deployment, **and** `HARBORRAG_EMBED_PROVIDER`, `HARBORRAG_EMBED_MODEL`, and
`HARBORRAG_EMBED_API_KEY` for its embedding deployment. All six are required: references
expand eagerly, so filling in only the chat three fails at load time. The production
example uses provider variables such as `AZURE_OPENAI_API_KEY` instead.

### Optional memory model

The conversation-memory layer (query rewriting, fact extraction and session summaries)
can use a cheaper model than chat. Add a `memory` logical model under `chat.models` in
`config/models/models.yaml` and set the three `HARBORRAG_MEMORY_*` variables in
`env/.env.models`:

```yaml
    memory:
      deployments:
        - name: openai-memory
          provider: ${HARBORRAG_MEMORY_PROVIDER}
          model: ${HARBORRAG_MEMORY_MODEL}
          api_key: ${HARBORRAG_MEMORY_API_KEY}
          capabilities:
            streaming: false
            structured_output: true
            json_mode: true
            tools: false
```

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
- Budget windows (`rpm_limit`, `tpm_limit`, `daily_cost_usd`, `monthly_cost_usd`) are counted per process: with several workers each one can use the whole budget, so size the limits per worker. Startup logs a warning when budgets run beside Redis-backed services.
- `max_request_cost_usd` needs a cost estimator supplied in code; set in YAML alone, the client refuses to start.
- Do not put credentials in committed YAML, headers, fixtures, or rendered output.

For paid, credentialed checks, use the model smoke scripts documented in [Testing](../../developers/testing/README.md#real-system-smoke-checks).
