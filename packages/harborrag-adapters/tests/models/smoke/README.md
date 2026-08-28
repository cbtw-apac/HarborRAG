# Live model smoke checks

These standalone scripts make small, real chat, embedding, and reranking calls
through the public Harbor clients. They verify provider authentication, LiteLLM
transport wiring, Harbor response normalization, and a few provider-neutral
output invariants.

These are not pytest tests. They never mock LiteLLM or a provider transport and
may consume paid API quota. Run one family first, using a least-privilege test
credential, before running the group script.

The shared safety rules and release-evidence guidance are in the
[real smoke-test runbook](../../README.md#real-system-smoke-tests).

## Prerequisites

- Run commands from the repository root.
- Use Python 3.12.
- Install the adapter's model and test dependencies.
- Ensure the machine can reach the selected provider or LiteLLM proxy.
- Review the provider's pricing, quota, and model availability.

With `uv`, create or update the project environment with:

```bash
uv sync --package harborrag-adapters --extra llm --extra test
```

You can then use `uv run` in place of `python` in the examples below if the
virtual environment is not activated.

## Quick config (no dotenv file)

Each script (`chat.py`, `embed.py`, `rerank.py`) has a "Quick config" block
near the top with plain constants, e.g. `EMBED_MODEL`, `EMBED_API_KEY`,
`BATCH`. Set the ones you need directly in the file and run the script - no
`.env` file required. Leave a constant as `None` to fall back to the matching
environment variable / dotenv value described below. Constants set in the
file always take precedence over the environment.

## Configure a dotenv file

Copy only the model families you intend to exercise from the repo-root
`env-example/.env.models.example`. Do not commit the populated file.

The loader checks these sources in this order:

1. Variables already exported in the process environment.
2. The file selected by `HARBOR_SMOKE_ENV_FILE`.
3. The repo-root `.env` when `HARBOR_SMOKE_ENV_FILE` is unset.

Exported variables are never overwritten by values in the dotenv file. A file
such as `env/.env.models` is not discovered automatically; select it explicitly:

```bash
export HARBOR_SMOKE_ENV_FILE=env/.env.models
```

PowerShell equivalent:

```powershell
$env:HARBOR_SMOKE_ENV_FILE = "env/.env.models"
```

You can also select the file for a single command:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.models \
  python packages/harborrag-adapters/tests/models/smoke/chat.py
```

### Minimum configuration

Every enabled family requires a provider, a provider model identifier, and a
usable authentication method.

Chat with an explicit API key:

```dotenv
HARBOR_CHAT_PROVIDER=openai
HARBOR_CHAT_MODEL=openai/REPLACE_WITH_REAL_CHAT_MODEL
HARBOR_CHAT_API_KEY=REPLACE_WITH_REAL_CHAT_API_KEY
```

Embeddings with an explicit API key:

```dotenv
HARBOR_EMBED_PROVIDER=openai
HARBOR_EMBED_MODEL=openai/REPLACE_WITH_REAL_EMBEDDING_MODEL
HARBOR_EMBED_API_KEY=REPLACE_WITH_REAL_EMBEDDING_API_KEY
HARBOR_EMBED_SPACE=smoke-embedding-space-v1
# HARBOR_EMBED_EXPECTED_DIMENSIONS=1536
```

Reranking with an explicit API key:

```dotenv
HARBOR_SMOKE_RERANK_PROVIDER=cohere
HARBOR_SMOKE_RERANK_MODEL=cohere/REPLACE_WITH_REAL_RERANK_MODEL
HARBOR_SMOKE_RERANK_API_KEY=REPLACE_WITH_REAL_RERANK_API_KEY
```

Replace all `REPLACE_WITH_REAL...` values. Placeholder values are rejected as
not configured. Families omitted from the file are skipped by `run_all.py` and
reported with exit code `2` when run individually.

For cloud providers that use ambient identity, omit the API key only when the
provider supports it and opt in explicitly:

```dotenv
HARBOR_CHAT_ALLOW_AMBIENT_CREDENTIALS=true
```

Provider-specific fields are prefixed with the family name. Supported fields
include `API_BASE`, `API_VERSION`, `DEPLOYMENT_NAME`, `CUSTOM_LLM_PROVIDER`,
`HEADERS_JSON`, `EXTRA_LITELLM_PARAMS_JSON`, AWS credentials/role fields, and
Vertex project, location, and credentials fields. See
`env-example/.env.models.example` for
the complete list and spelling.

### Chat backend selection

Chat defaults to the `direct_sdk` backend. Select a backend with:

```dotenv
HARBOR_CHAT_BACKEND=direct_sdk
```

Supported values are:

- `direct_sdk`: call the configured provider through LiteLLM directly.
- `litellm_router`: exercise Harbor's LiteLLM Router integration.
- `litellm_proxy`: call a running LiteLLM Proxy deployment.

Proxy mode additionally requires:

```dotenv
HARBOR_CHAT_PROXY_API_BASE=https://proxy.example.com
HARBOR_CHAT_PROXY_API_KEY=REPLACE_WITH_REAL_PROXY_KEY
# HARBOR_CHAT_PROXY_HEADERS_JSON={}
```

The default request timeout is 90 seconds. Override it for all families with a
positive numeric value:

```dotenv
HARBOR_SMOKE_TIMEOUT_SECONDS=120
```

## Run the checks

Run a single configured family first:

```bash
python packages/harborrag-adapters/tests/models/smoke/chat.py
python packages/harborrag-adapters/tests/models/smoke/embed.py
python packages/harborrag-adapters/tests/models/smoke/rerank.py
```

After the individual targets pass, run every configured family:

```bash
python packages/harborrag-adapters/tests/models/smoke/run_all.py
```

When using `uv` without activating the environment:

```bash
uv run --package harborrag-adapters --extra llm --extra test \
  python packages/harborrag-adapters/tests/models/smoke/chat.py
```

The checks deliberately use one attempt with no deployment failover or model
fallback, limiting both cost and ambiguity during diagnosis.

## What success verifies

| Target | Real operation | Required result |
| --- | --- | --- |
| Chat | Sends a two-message prompt | Non-empty text, provider model, and request metadata |
| Embed | Embeds two short strings as one batch | Two finite vectors with consistent positive dimensions |
| Rerank | Reranks three documents and requests the top two | Two unique in-range indexes with finite relevance scores |

Successful output contains only safe metadata such as the provider model,
latency, dimensions, or result count. It does not print prompts, responses,
embedding vectors, document text, credentials, headers, or raw provider bodies.

## Exit codes

| Code | Meaning | Next action |
| --- | --- | --- |
| `0` | The real provider operation passed | Record sanitized evidence if required |
| `1` | Configuration was present, but validation or the provider call failed | Investigate the sanitized error |
| `2` | The family is missing or still contains a placeholder | Configure it, or treat it as intentionally pending |

`run_all.py` ignores code `2` for individual unconfigured families. It returns
`0` when every configured family passes, `1` when any configured family fails,
and `2` when no family is configured.

## Troubleshooting

### The script says `not configured`

- Confirm both `HARBOR_SMOKE_<FAMILY>_PROVIDER` and
  `HARBOR_SMOKE_<FAMILY>_MODEL` are present.
- Replace every `REPLACE_WITH_REAL...` placeholder.
- If the file is not repo-root `.env`, set `HARBOR_SMOKE_ENV_FILE` to its path.
- Check for an already exported variable: process variables take precedence
  over the dotenv file, including stale or empty-looking shell configuration.

### Imports or optional dependencies fail

Re-run the `uv sync` command from the repository root. Confirm that the command
uses Python 3.12 and the same environment where `harborrag-adapters`,
`harborrag-core`, LiteLLM, Pydantic, and PyYAML were installed.

### Authentication, model, or endpoint errors

- Verify that the key is authorized for the exact provider and model.
- Use the provider's LiteLLM model naming convention.
- For OpenAI-compatible gateways, set `API_BASE` and, when required,
  `CUSTOM_LLM_PROVIDER` or custom headers.
- For Azure-style deployments, verify `API_BASE`, `API_VERSION`, and
  `DEPLOYMENT_NAME` independently of the logical model name.
- For `litellm_proxy`, verify the separate proxy URL and proxy key variables.

### The request times out or appears to hang

Check DNS, firewall, VPN, proxy, and provider status from the same shell. LiteLLM
may attempt to refresh its model-cost metadata before the provider operation;
if that fetch fails, it normally warns and falls back to its packaged data.
Provider request timeouts do not necessarily cap import-time network activity.

### Embedding dimensions fail validation

Remove `HARBOR_EMBED_EXPECTED_DIMENSIONS` to discover the returned size,
or set it to the positive dimension guaranteed by the selected provider model.
Do not reuse an embedding-space name across models that produce incompatible
vector spaces.

## Credential and output safety

- Use test credentials with the minimum required permissions and quota.
- Keep populated dotenv files ignored and outside source control.
- Never paste the dotenv file, raw headers, or provider response bodies into
  test evidence or an issue.
- The scripts redact common credential formats from errors, but review all
  captured output before sharing it.
