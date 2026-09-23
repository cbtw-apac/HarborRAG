# Chat models

Which model answers a chat turn, and how a tenant overrides the shared
catalog. For the request and response contract see [Chat](README.md).

## Configure the model

The checked-in runtime catalog uses one logical model named `primary`:

```yaml
chat:
  default_model: primary
  models:
    primary:
      deployments:
        - name: openai-primary
          provider: ${HARBOR_CHAT_PROVIDER}
          model: ${HARBOR_CHAT_MODEL}
          api_key: ${HARBOR_CHAT_API_KEY}
```

Copy the environment template, replace its placeholders, and keep the
populated file out of version control:

```bash
cp env-example/.env.models.example env/.env.models
```

The relevant values are `HARBOR_CHAT_PROVIDER`, `HARBOR_CHAT_MODEL`, and
`HARBOR_CHAT_API_KEY`. `HARBORRAG_MODEL_CONFIG_PATH` selects a different model
catalog. Configuration loading expands environment references but does not
load `.env` files itself; the deployment scripts and Compose services load
`env/.env.models` for you.

Callers may name one logical model (see [Per-tenant models](#per-tenant-models))
and one system prompt. They do not accept provider credentials, base URLs,
custom headers, tools, provider-specific parameters, or sampling overrides.
Temperature and token limits come entirely from the catalog.

## Per-tenant models

By default there is one catalog for the whole process: `config/models.yaml`,
whose API keys expand from the process environment. Every tenant answers from
it, so bringing a new model or rotating a key means a redeploy.

Set `HARBORRAG_CHAT_TENANT_CATALOGS_ENABLED=true` and a tenant's own stored
configuration overrides that shared catalog for its own turns. It is off by
default: switching it on moves chat credentials into the control database and
gives each configured tenant its own client, so it is an explicit operator
choice rather than something a schema change turns on underneath a running
deployment.

A tenant's models are its `providers` rows with `family = "chat"`. Each row is
one deployment. Its `config_json` accepts exactly these keys:

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

`provider`, `logical_model`, and `model` are required; the rest are optional.
Several rows sharing one `logical_model` become several deployments behind
that one name, and their `deployment` values must differ. `default` marks the
tenant's default model. An unknown key is an error naming the offending row.

The API key is never in `config_json`. It lives in the row's `secret_ref`,
which points at the encrypted control-plane secret store and is resolved at
request time scoped to the owning tenant, so a ref that leaks across a tenant
boundary resolves to nothing.

What the tenant does **not** own is everything outside its models: timeouts,
retry and failover policy, routing strategy, response caching, telemetry
privacy, and `chat.security`. Those stay exactly as `config/models.yaml` set
them, which is what keeps a stored row from naming a provider or an endpoint
the operator has not allowed. `extra` keys that are not deployment fields are
passed to the provider SDK as extension parameters and are still bounded by
the operator's `chat.security.allowed_extra_litellm_params`.

## How a change takes effect

There is no restart and no cross-replica invalidation channel. Every request
asks the control plane for a cheap fingerprint over the tenant's rows and
compares it with the fingerprint the cached client was built from. The same
fingerprint reuses the cached client; a different one rebuilds it and disposes
the old one. So a rotated key, a new model, or a removed deployment takes
effect on the tenant's next request, in every replica, without a redeploy.

Clients are cached per tenant up to `HARBORRAG_CHAT_TENANT_CLIENT_CACHE_SIZE`
(default 32). Each holds a connection pool, so the least recently used client
is disposed when the bound is reached and rebuilt if that tenant returns.

## When a tenant's configuration is broken

Chat never fails because a tenant misconfigured itself. If the rows cannot be
projected onto a valid client -- an unlisted provider, an endpoint outside the
allowlist, a capability the provider does not support, a `secret_ref` that no
longer resolves -- or if the control plane is unreachable, the turn is answered
from the shared catalog and the failure is logged at ERROR naming the tenant
and the reason. The tenant loses its own models for that turn, not its ability
to chat. Its model names are then bounded by the shared catalog, since the
shared client is what actually serves the request.

One malformed row is skipped on its own: the rest of the tenant's catalog is
built normally and the skipped row is logged with its id.

## Choosing a model per request

`POST /v1/chat/completions` and `POST /v1/agent/completions` accept an
optional `model`:

```json
{"tenant": "acme", "session_id": "session-...", "prompt": "...", "model": "fast"}
```

Omitting it resolves the catalog's default, exactly as before. When the tenant
has its own catalog the name must be one that catalog configured; otherwise it
must exist in `config/models.yaml`. A name outside that bound is rejected
before the turn starts with `422` and
`{"code":"harbor_validation_error","details":{"field":"model"}}` -- never a
provider error, and never a silent fall back to the default. Streaming
requests are checked the same way, before any frame is written, so a rejected
name is one status code rather than an error mid-body.

The response reports the model that actually answered in its existing `model`
field. `POST /v1/agent/runs/{run_id}/resume` takes no `model`: a run continues
under the model it started with.

## See also

- [Chat](README.md) - the HTTP and CLI surfaces
- [Chat models](models.md) - the model catalog and per-tenant overrides
- [Conversation memory](memory.md) - what a turn remembers and for how long
- [Agent](agent.md) - the bounded multi-turn surface
- [Limits and accounting](limits.md) - deadlines, admission control, usage
