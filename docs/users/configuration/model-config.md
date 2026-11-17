# Model Configuration

HarborRAG calls three kinds of model. A chat model writes answers, and also extracts
entities and summaries for the knowledge graph. An embedding model turns text into
vectors, the numbers that semantic search compares. A reranking model reorders
search results by how well they answer the question. The model catalog,
[`config/models/models.yaml`](../../../config/models/models.yaml), says which models
those are and how to reach them. This page explains the shipped catalog, how to point
it at another provider, and how to check a catalog before anything calls a provider.

The catalog has one section per family, `chat`, `embed` and `rerank`, and uses two
terms throughout:

- A **logical model**, such as `primary`, is the stable name the application asks for.
- A **deployment** is one concrete provider model behind a logical model: its
  provider, the provider's model name, credentials and verified capabilities. A
  logical model may have several deployments, aliases and ordered fallbacks.

The `config/models/` folder holds:

- [`models.yaml`](../../../config/models/models.yaml), the active catalog, which sends
  chat, embedding and reranking through one LiteLLM proxy gateway;
- [`models.production.example.yaml`](../../../config/models/models.production.example.yaml),
  a catalog that calls providers directly, with routing, fallbacks, reranking and
  profiles.

`HARBORRAG_MODEL_CONFIG_PATH` selects another catalog file.

## The shipped gateway catalog

A LiteLLM proxy gateway is a server that offers many model providers behind one
OpenAI-compatible API and one key. HarborRAG does not run the gateway; point it at one
you run or can use. LiteLLM explains how to run one in
[LiteLLM Proxy Server](https://docs.litellm.ai/docs/simple_proxy).

The repository's `config/models/models.yaml` defines one logical model named `primary`
in each family, and every one of them calls the same gateway:

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

The catalog reads five variables: `HARBORRAG_URL`, `HARBORRAG_API_KEY`,
`HARBORRAG_CHAT_MODEL`, `HARBORRAG_EMBED_MODEL` and `HARBORRAG_RERANKER_MODEL`.
[Model gateway environment](config-file-reference.md#model-gateway-environment)
describes each one. In a repository checkout they go in `env/.env.models`, created
from `env-example/.env.models.example`. All five are required: references expand
eagerly, so leaving out the reranker model fails at load time even if you never switch
reranking on.

Do not confuse `HARBORRAG_EMBED_MODEL` with `HARBORRAG_EMBEDDING_MODEL`. The first is
the gateway's name for the embedding model. The second, which you rarely need, selects
a logical model of the catalog's `embed` section, such as `primary`.

Changing the embedding model needs a re-index. Vectors written by the previous model
have the same 1536 dimensions, so nothing reports an error: retrieval quality drops
without warning until every source is re-ingested with `--force-reprocess` (or
`harborrag source sync PROJECT/CONNECTION --mode force`).

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
logical model the layer uses (`memory` by default); while no such model exists, the
layer falls back to the default chat model.

## Call a provider directly

To skip the gateway, write deployments that name the provider itself. Get the
provider's key from its own documentation, list the provider in the family's
`security.allowed_providers`, and keep the key in an environment variable. Every
family the application uses needs a section: chat for answers, `embed` for ingestion
and retrieval, and `rerank` only for reranking.

<details markdown="1"><summary>OpenAI</summary>

The smallest chat family that calls one provider directly:

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

</details>

<details markdown="1"><summary>Azure OpenAI, with fallbacks and profiles</summary>

[`models.production.example.yaml`](../../../config/models/models.production.example.yaml)
routes chat across two Azure OpenAI deployments with an Anthropic fallback, embeds
with Azure OpenAI, reranks with Amazon Bedrock with a Cohere fallback, and defines
`development` and `production` profiles. One of its chat deployments:

```yaml
        - name: azure-east
          provider: azure_openai
          model: azure/REPLACE_WITH_AZURE_DEPLOYMENT
          deployment_name: REPLACE_WITH_AZURE_DEPLOYMENT
          api_key: ${AZURE_OPENAI_API_KEY}
          api_base: ${AZURE_OPENAI_ENDPOINT}
          api_version: ${AZURE_OPENAI_API_VERSION}
          weight: 3
```

It reads `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`,
`ANTHROPIC_API_KEY`, `COHERE_API_KEY` and `AWS_REGION`.

</details>

`env-example/.env.models.example` also keeps commented `HARBORRAG_CHAT_*` and
`HARBORRAG_EMBED_*` examples for such a catalog. They stay commented because they set
`HARBORRAG_CHAT_MODEL` and `HARBORRAG_EMBED_MODEL`, which the gateway values already
use.

## Environment and secret references

`${NAME}` is required. `${NAME:-default}` supplies a default; reserve it for values
that are not secret. References expand while the catalog loads, so a missing required
variable fails validation before any provider call.

The catalog loaders do not read `.env` files. In a checkout the CLI loads
`env/.env.models` itself and Compose passes it to the containers; anywhere else,
export the variables, pass the file with `uv run --env-file`, or use a secret manager.

A validated configuration can also come from a Python mapping with `from_dict(...)`,
and a secret manager can provide a `SecretResolver` instead of plaintext values.

## Profiles and precedence

When the file has a top-level `profiles` mapping, pass `profile="name"` to
`from_file`, or `--profile` to the commands below. A family's configuration is layered
as:

```text
family base < selected profile < code overrides < request fields
```

Nested mappings merge recursively; lists and scalar values replace the lower-precedence
value.

## Check a catalog before you use it

These commands check structure and variable resolution without calling a provider.
They do not prove that a key or model name works.

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models validate config/models/models.yaml --family chat
```

You should see:

```text
valid chat configuration: config/models/models.yaml
```

Run it with `--family embed` and `--family rerank` too. `explain` prints the resolved
configuration of one family as JSON, and `render` prints a sanitized copy as JSON or
YAML (`--format`, `--output`):

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models/models.yaml --family embed

AWS_REGION=us-east-1 COHERE_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models render \
    config/models/models.production.example.yaml --family rerank --format yaml
```

`harborrag doctor` also loads the catalog: its `models catalog` check fails and names
any variable the catalog references that is blank.

## How the application uses the catalog

The HTTP API, the CLI and the MCP server do not build model clients themselves. The
runtime loads the `chat` family from `HARBORRAG_MODEL_CONFIG_PATH` when first needed,
owns one asynchronous client, and exposes it through `HarborRAG.chat`. Callers may
select a logical model, but cannot provide a deployment, credential, base URL, header,
tool definition or provider-specific parameter. To choose the model for a chat turn,
see [Chat models](../chat/models.md).

Stored prompts are separate from model configuration. The runtime packages the
`default` and `concise` Markdown prompt templates; see [Chat](../chat/README.md).

## Load a family from Python

Outside the repository's environment, install `harborrag-adapters[llm]` first.

```python
from harborrag_adapters.models.chat import ChatClientFactory, HarborChatClientConfig

config = HarborChatClientConfig.from_file("config/models/models.yaml")
with ChatClientFactory.create(config) as client:
    response = client.chat([{"role": "user", "content": "Summarize HarborRAG."}])
    print(response.text)
```

Embedding and reranking use `HarborEmbedClientConfig`/`HarborEmbedClient` and
`HarborRerankClientConfig`/`HarborRerankingClient`.

## Reliability and safety controls

The catalog supports timeouts, staged retry and failover, routing, circuit breakers,
per-deployment concurrency, caching, singleflight, budgets, health state, connection
pools, observability, and provider and base-URL allowlists. Chat also selects the
`direct_sdk`, `litellm_router` or `litellm_proxy` transport.

- Declare only capabilities verified for the concrete provider model.
- Keep embedding fallbacks in the same embedding space and dimension.
- Opt in deliberately to custom providers, endpoint hosts, request auth headers,
  sensitive caching, and prompt and response logging.
- Keep tenant identifiers in request metadata when tenant-isolated cache, budget or
  routing features require them.
- Keep credentials out of committed YAML, headers, fixtures and rendered output.

For paid, credentialed checks, use the model smoke scripts in
[Real-system smoke checks](../../developers/testing/README.md#real-system-smoke-checks).
