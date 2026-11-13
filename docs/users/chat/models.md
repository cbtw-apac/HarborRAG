# Chat models

Find out which model answers a chat turn, what a caller may choose, and how a
tenant can answer from its own models instead of the shared catalog. A
**logical model** is a name such as `primary` that the model catalog maps to
one or more concrete provider deployments; callers only ever name logical
models.

For the request and response contract, see [Chat](README.md).

## The shared chat model

Every chat turn answers from the `chat` family of the model catalog,
`config/models/models.yaml` (`HARBORRAG_MODEL_CONFIG_PATH` selects another
file). The shipped catalog has one logical chat model, `primary`, which calls a
[LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy) gateway as an
OpenAI-compatible endpoint. For chat it reads three values from
`env/.env.models`:

- `HARBORRAG_URL`: the gateway's base URL, without `/v1`.
- `HARBORRAG_API_KEY`: the gateway key.
- `HARBORRAG_CHAT_MODEL`: the gateway's name for the chat model.

The catalog also needs the embedding and reranker values, and every reference
must be set for it to load. The catalog layout, the other families and all five
variables are described in
[The shipped gateway catalog](../configuration/model-config.md#the-shipped-gateway-catalog)
and [Model gateway environment](../configuration/config-file-reference.md#model-gateway-environment).
To run a gateway, follow the LiteLLM proxy documentation linked above.

What a caller may set on a turn:

- HTTP and the CLI cannot choose a model; see
  [Choosing a model per turn](#choosing-a-model-per-turn).
- SDK callers may name one logical model and one system prompt.
- No caller can pass provider credentials, base URLs, custom headers, tools,
  provider-specific parameters or sampling overrides. Temperature and token
  limits come only from the catalog.

## Per-tenant models

By default the whole process has one catalog, `config/models/models.yaml`,
whose API keys expand from the process environment. Every tenant answers from
it, so a new model or a rotated key means a redeploy.

Set `HARBORRAG_CHAT_TENANT_CATALOGS_ENABLED=true` and a tenant's own stored
models replace the shared catalog for that tenant's turns. It is off by
default: switching it on moves chat credentials into the control database and
gives each configured tenant its own client, so it is an explicit operator
choice rather than something a schema change turns on under a running
deployment.

A tenant's models are its `providers` rows with `family = "chat"` in the
control database. This release has no HTTP route or CLI command that writes
these rows. Each row is one deployment, and its `config_json` accepts exactly
these keys:

```json
{
  "provider": "openai",
  "logical_model": "fast",
  "model": "gpt-4o-mini",
  "deployment": "fast-eu",
  "api_base": "https://example.openai.azure.com",
  "capabilities": {"streaming": true},
  "weight": 2,
  "default": true,
  "extra": {"organization": "acme"}
}
```

- `provider`, `logical_model` and `model` are required; the rest are optional.
- With the shipped catalog a row must name `"provider": "openai"` or
  `"provider": "openai_compatible"`, the providers its `chat.security` allows;
  a row naming any other provider is refused, as described in
  [When a tenant's configuration is broken](#when-a-tenants-configuration-is-broken).
- Several rows sharing one `logical_model` become several deployments behind
  that one name, and their `deployment` values must differ.
- `default: true` marks the tenant's default model.
- An unknown key is an error that names the row.
- A `routing_rules` row of family `chat` that points at a deployment can
  override its `weight`, as `{"weight": 3}`.

The API key is never in `config_json`. It lives in the row's `secret_ref`,
which points at the encrypted control-plane secret store and is resolved at
request time within the owning tenant, so a ref that leaks across a tenant
boundary resolves to nothing.

The tenant does **not** own anything outside its models: timeouts, retry and
failover policy, routing strategy, response caching, telemetry privacy, and
`chat.security` stay exactly as `config/models/models.yaml` sets them. That is
what keeps a stored row from naming a provider or an endpoint the operator has
not allowed. `extra` keys that are not deployment fields are passed to the
provider SDK as extension parameters, still bounded by the operator's
`chat.security.allowed_extra_litellm_params`.

## How a tenant change takes effect

A change needs no restart and no cross-replica message. On every request the
API asks the control plane for a cheap fingerprint of the tenant's rows and
compares it with the fingerprint its cached client was built from. The same
fingerprint reuses the cached client; a different one rebuilds it and disposes
of the old one. A rotated key, a new model or a removed deployment therefore
takes effect on the tenant's next request, in every replica.

Clients are cached per tenant, up to `HARBORRAG_CHAT_TENANT_CLIENT_CACHE_SIZE`
(default `32`). Each holds a connection pool, so the least recently used client
is disposed of when the cache is full and rebuilt if that tenant returns.

## When a tenant's configuration is broken

Chat never fails because a tenant misconfigured its models. If the rows cannot
be turned into a valid client (an unlisted provider, an endpoint outside the
allowlist, a capability the provider does not support, a `secret_ref` that no
longer resolves), or if the control plane is unreachable, the turn is answered
from the shared catalog and the failure is logged at `ERROR` with the tenant
and the reason. The tenant loses its own models for that turn, not its ability
to chat, and its model names are then limited to the shared catalog's.

One malformed row is skipped on its own: the rest of the tenant's catalog is
built normally and the skipped row is logged with its ID.

## Choosing a model per turn

`POST /v1/chat/completions` takes no `model`: every HTTP and CLI turn answers
from the tenant's default model, which is its own catalog's default, else the
shared catalog's. SDK callers can name one logical model in
`ChatExecutionOptions.model`. When the tenant has its own catalog, the name
must be one that catalog configures; otherwise it must exist in
`config/models/models.yaml`. Any other name is a `validation_error` before any
model call, never a silent fallback to the default.

## See also

- [Chat](README.md): the HTTP and CLI surfaces.
- [Conversation memory](memory.md): what a turn remembers and for how long.
- [Limits and accounting](limits.md): deadlines, admission control and usage.
- [Model configuration](../configuration/model-config.md): the shared catalog.
