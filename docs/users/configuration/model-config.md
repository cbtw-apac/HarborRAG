# Model Configuration

HarborRAG calls up to three kinds of model. A chat model writes answers, and also
extracts entities and summaries for the knowledge graph. An embedding model turns text
into vectors, the numbers that semantic search compares. A reranking model, which is
optional, reorders search results by how well they answer the question. The model
catalog, [`config/models/models.yaml`](../../../config/models/models.yaml), says which
models those are and how to reach them. This page explains the shipped catalog, how to
point it at another endpoint or provider, and how to check a catalog before anything
calls a provider.

The catalog has one section per family: `chat`, `embed` and, when reranking is
configured, `rerank`. It uses two terms throughout:

- A **logical model**, such as `primary`, is the stable name the application asks for.
- A **deployment** is one concrete provider model behind a logical model: its
  provider, the provider's model name, credentials and verified capabilities. A
  logical model may have several deployments, aliases and ordered fallbacks.

The `config/models/` folder holds:

- [`models.yaml`](../../../config/models/models.yaml), the active catalog, which calls
  one OpenAI-compatible endpoint for chat, embedding and optional reranking: OpenAI by
  default, or a gateway;
- [`models.production.example.yaml`](../../../config/models/models.production.example.yaml),
  a catalog that calls providers directly, with routing, fallbacks and reranking.

`HARBORRAG_MODEL_CONFIG_PATH` selects another catalog file.

## The shipped catalog

The repository's `config/models/models.yaml` serves OpenAI and an OpenAI-compatible
gateway, such as a LiteLLM proxy, from one file. It defines one logical model named
`primary` for chat, one for embedding and an optional one for reranking. Each family
has its own model name and key. All of them call one endpoint: OpenAI's,
`https://api.openai.com/v1`, unless `HARBORRAG_URL` names another.

- **Chat** uses the `direct_sdk` backend with one `provider: openai_compatible`
  deployment at the endpoint's URL. Topology extraction accepts only the
  `direct_sdk` backend, which is why chat does not use the `litellm_proxy` backend.
  `chat.security.allowed_providers` is `[openai, openai_compatible]`. The deployment
  does not declare `reasoning_effort`: given a reasoning effort, a Claude model behind
  a gateway answers in prose instead of the requested JSON schema, and topology
  extraction and graph summaries need schema-constrained answers.
- **Embedding** uses one deployment that sends an OpenAI-format request. Its provider
  is `openai_compatible` unless `HARBORRAG_EMBED_PROVIDER` is `cohere`. With `cohere`,
  HarborRAG also sends Cohere's `input_type`: `search_query` for questions and
  `search_document` for chunks. The catalog expects 1536-dimension vectors, which
  OpenAI's `text-embedding-3-small` and Cohere's embed v4 write. The embedding space
  is named after `HARBORRAG_EMBED_MODEL`.
- **Reranking** is off until `HARBORRAG_RERANKER_MODEL` names a model. Chat retrieval
  then uses it when `retrieval.rerank` is on; see
  [Runtime settings](runtime-settings.md#rerank-chat-retrieval).

The catalog reads four required variables, `HARBORRAG_CHAT_MODEL`,
`HARBORRAG_CHAT_API_KEY`, `HARBORRAG_EMBED_MODEL` and `HARBORRAG_EMBED_API_KEY`, and
four optional ones, `HARBORRAG_URL`, `HARBORRAG_EMBED_PROVIDER`,
`HARBORRAG_RERANKER_MODEL` and `HARBORRAG_RERANKER_API_KEY`.
[Model endpoint environment](config-file-reference.md#model-endpoint-environment)
describes each one. In a repository checkout they go in `env/.env.models`, created
from `env-example/.env.models.example`. References expand eagerly, so a missing
embedding model name fails at load time even if you only chat.

Do not confuse `HARBORRAG_EMBED_MODEL` with `HARBORRAG_EMBEDDING_MODEL`. The first is
the endpoint's name for the embedding model. The second, which you rarely need, selects
a logical model of the catalog's `embed` section, such as `primary`.

Changing the embedding model needs a re-index. Vectors written by the previous model
have the same 1536 dimensions, so nothing reports an error: retrieval quality drops
without warning until every connection is re-ingested in `force` mode, for example
with `harborrag ingestions start default/harborrag-workspace --mode force`.

### Use another OpenAI-compatible endpoint

Set `HARBORRAG_URL` to the endpoint's base URL, the address that its
`/chat/completions` and `/embeddings` paths follow. Put that endpoint's key and model
name in each family's variables. The model names are the ones the endpoint lists at
its `/models` path, without a provider prefix such as `openai/`. A LiteLLM proxy
gateway is such an endpoint: a server that offers many model providers behind one
OpenAI-compatible API. HarborRAG does not run a gateway; LiteLLM explains how to run
one in [LiteLLM Proxy Server](https://docs.litellm.ai/docs/simple_proxy).

```text
HARBORRAG_URL=https://gateway.example.com
HARBORRAG_CHAT_MODEL=<the gateway's chat model name>
HARBORRAG_CHAT_API_KEY=<gateway key>
HARBORRAG_EMBED_MODEL=<the gateway's embedding model name>
HARBORRAG_EMBED_API_KEY=<gateway key>
```

Every family uses the endpoint `HARBORRAG_URL` names. To send chat and embedding to
different endpoints, give each deployment its own `api_base` in a catalog of your own.

The embedding model behind the endpoint must write 1536-dimension vectors, or the
catalog's `expected_dimensions` must change with it.

### Send Cohere embeddings and reranking through a gateway

A Cohere embedding model needs to know whether a text is a question or a chunk, and an
OpenAI-format request does not say. Add one line so HarborRAG sends it:

```text
HARBORRAG_EMBED_PROVIDER=cohere
```

Leave the line out for an OpenAI embedding model: `input_type` is not part of OpenAI's
embeddings API.

To rerank chat passages, name the gateway's rerank model and its key:

```text
HARBORRAG_RERANKER_MODEL=<the gateway's rerank model name>
HARBORRAG_RERANKER_API_KEY=<gateway key>
```

Set the key whenever the gateway needs one. The shipped catalog's `litellm_proxy`
provider needs no key, so the services start without it. A gateway that requires a key
then refuses each rerank call, and chat fails while `retrieval.rerank` is on. A catalog
of your own whose rerank provider always needs a key, such as `cohere`, is refused
instead, with an error that names the empty variable. The three keys may be the same
gateway key. Restart the API, the worker and the MCP server after you change
`env/.env.models`.

### Optional memory model

The conversation-memory layer (query rewriting, fact extraction and session summaries)
can use a cheaper model than chat. Add a `memory` logical model under `chat.models` in
`config/models/models.yaml` and set `HARBORRAG_MEMORY_MODEL` in `env/.env.models` to
the endpoint's name for that model:

```yaml
    memory:
      deployments:
        - name: openai-compatible-memory
          provider: openai_compatible
          model: ${HARBORRAG_MEMORY_MODEL}
          api_base: ${HARBORRAG_URL:-https://api.openai.com/v1}
          api_key: ${HARBORRAG_CHAT_API_KEY}
          capabilities:
            streaming: false
            structured_output: true
            json_mode: true
            tools: false
```

The memory model calls the same endpoint as the `primary` chat model, with the same URL
and key. `HARBORRAG_MEMORY_PROVIDER` and `HARBORRAG_MEMORY_API_KEY` belong to a catalog
that calls a provider directly.

Extraction and summarization need schema-constrained answers, so the deployment must
declare `structured_output` and `json_mode`. `HARBORRAG_MEMORY_MODEL_PROFILE` names the
logical model the layer uses (`memory` by default); while no such model exists, the
layer falls back to the default chat model.

## Call a provider directly

To call a provider through its own API instead of an OpenAI-compatible endpoint,
write deployments that name the provider itself. Get the provider's key from its own
documentation, list the provider in the family's `security.allowed_providers`, and
keep the key in an environment variable. Every family the application uses needs a
section: `chat` for answers, `embed` for ingestion and retrieval, and `rerank` only
for reranking.

<details markdown="1">
<summary>OpenAI</summary>

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

<details markdown="1">
<summary>Azure OpenAI, with fallbacks</summary>

[`models.production.example.yaml`](../../../config/models/models.production.example.yaml)
routes chat across two Azure OpenAI deployments with an Anthropic fallback, embeds
with Azure OpenAI, and reranks with Amazon Bedrock with a Cohere fallback. One of its
chat deployments:

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
`HARBORRAG_CHAT_MODEL` and `HARBORRAG_EMBED_MODEL`, which the shipped catalog already
uses.

## Environment and secret references

`${NAME}` is required. `${NAME:-default}` supplies a default; reserve it for values
that are not secret. References expand while the catalog loads, so a missing required
variable fails validation before any provider call.

The catalog loaders do not read `.env` files. In a checkout the CLI loads
`env/.env.models` itself and Compose passes it to the containers; anywhere else,
export the variables, pass the file with `uv run --env-file`, or use a secret manager.

A validated configuration can also come from a Python mapping with `from_dict(...)`,
and a secret manager can provide a `SecretResolver` instead of plaintext values.
`from_file` and `from_dict` also take `overrides`, a mapping merged over the family's
section: nested mappings merge recursively, and lists and scalar values replace the
catalog's value.

## What the catalog refuses

The loader and the model clients refuse values that would otherwise fail silently or
send a credential where it does not belong:

- **Duplicate and unknown keys.** A key written twice in one mapping is refused, in
  YAML and JSON. A catalog with a `chat`, `embed` or `rerank` section may hold no other
  top-level key, so a misspelled section fails instead of turning its family off. The
  `profiles:` section of earlier releases is refused the same way:
  `model configuration has unknown top-level key(s): profiles`.
- **Padding and line breaks.** `api_key`, `model`, `api_base` and `deployment_name`
  lose surrounding whitespace, such as the trailing carriage return of an env file with
  Windows line endings. A line break inside one is refused, and the error names the
  field. A model name that is blank after trimming fails when the catalog loads.
- **Blank keys.** A deployment whose `api_key` is empty or only whitespace is refused
  when its client is built, with
  `openai_compatible deployment 'openai-compatible-chat' has an empty api_key; set the variable it reads`.
  Otherwise LiteLLM would send an ambient key, such as `OPENAI_API_KEY`, to the
  configured endpoint. Only a rerank provider that needs no key and reads no ambient
  credentials may leave it empty, such as `litellm_proxy`, `vllm` or `infinity`. The
  `litellm_proxy` chat backend's own key, `chat.backend.proxy.api_key`, is trimmed the
  same way and must not be empty either; it fails when the catalog loads.
- **Credential headers.** A header whose name contains `key`, `token`, `auth`,
  `secret`, `signature` or `cookie`, in any case, counts as a credential: its value is
  masked in printed and rendered configuration. A request's own headers with such a
  name, for example `X-Goog-Api-Key`, `Idempotency-Key` or `X-CSRF-Token`, are refused
  unless `security.allow_request_auth_headers` is on, and the `litellm_proxy` chat
  backend refuses them always.

## Check a catalog before you use it

These commands check structure and variable resolution without calling a provider.
They do not prove that a key or model name works, and they do not build a client, so
they accept a blank key.

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models validate config/models/models.yaml --family chat
```

You should see:

```text
valid chat configuration: config/models/models.yaml
```

Run it with `--family embed` too. Check `--family rerank` only once
`HARBORRAG_RERANKER_MODEL` is set: with reranking off, the shipped rerank section names
no model, so `validate` exits 2 with `String should have at least 1 character`.

`explain` prints the resolved configuration of one family as JSON, and `render` prints
a sanitized copy as JSON or YAML (`--format`, `--output`):

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models/models.yaml --family embed

AWS_REGION=us-east-1 COHERE_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models render \
    config/models/models.production.example.yaml --family rerank --format yaml
```

`harborrag doctor` also loads the catalog: its `models catalog` check fails on a
catalog the loader refuses, and names each `${NAME}` reference whose variable is
blank. A reference with a default, such as `${HARBORRAG_RERANKER_API_KEY:-}`, is not
checked.

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
