# Model smoke checks

These standalone scripts send small, real chat, embedding and reranking requests through the
public Harbor clients. A passing run shows that the provider accepts your credentials, the LiteLLM
transport is wired correctly, and Harbor normalizes the response.

They are not pytest tests: they never mock LiteLLM or the provider, and they can spend paid quota.
Run one family with a least-privilege test credential before the group script, and read the shared
[safety rules and exit codes](../../README.md#real-system-smoke-tests) first.

## Before you start

- Run every command from the repository root.
- Use the workspace environment from [CONTRIBUTING](../../../../../CONTRIBUTING.md#development-setup);
  it includes LiteLLM, Pydantic and PyYAML. Do not narrow the sync to one package, because
  `uv sync` then removes the rest of the workspace from the environment.
- Make sure the machine can reach the provider or LiteLLM proxy, and check its pricing, quota and
  model availability.

## Give the scripts their settings

The scripts read their own variables, one set per family:

| Family | Variable prefix | Script |
| --- | --- | --- |
| Chat | `HARBORRAG_CHAT_` | `chat.py` |
| Embedding | `HARBORRAG_EMBED_` | `embed.py` |
| Reranking | `HARBOR_SMOKE_RERANK_` | `rerank.py` |

The application's model gateway values (`HARBORRAG_URL`, `HARBORRAG_API_KEY` and the gateway's
model names) do not configure a smoke family. `HARBORRAG_CHAT_MODEL` and `HARBORRAG_EMBED_MODEL`
are names the gateway values also use, so keep the smoke values in a separate file rather than in
`env/.env.models`, for example `env/.env.models-smoke` (everything under `env/` is git-ignored).
Copy the smoke examples from `env-example/.env.models.example` into it.

The scripts take each variable from, in order:

1. a variable already exported in your shell, which a file never replaces;
2. the file `HARBOR_SMOKE_ENV_FILE` names;
3. a `.env` file at the repository root, when `HARBOR_SMOKE_ENV_FILE` is unset.

Select the file for one command:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.models-smoke \
  uv run python packages/harborrag-adapters/tests/models/smoke/chat.py
```

<details markdown="1"><summary>PowerShell</summary>

```powershell
$env:HARBOR_SMOKE_ENV_FILE = "env/.env.models-smoke"
uv run python packages/harborrag-adapters/tests/models/smoke/chat.py
```

</details>

### Minimum settings per family

Each family needs a provider, the provider's model name and a way to authenticate. A value that
still contains `REPLACE_WITH_REAL` counts as not set.

Chat:

```dotenv
HARBORRAG_CHAT_PROVIDER=openai
HARBORRAG_CHAT_MODEL=openai/REPLACE_WITH_REAL_CHAT_MODEL
HARBORRAG_CHAT_API_KEY=REPLACE_WITH_REAL_CHAT_API_KEY
```

Embedding:

```dotenv
HARBORRAG_EMBED_PROVIDER=openai
HARBORRAG_EMBED_MODEL=openai/REPLACE_WITH_REAL_EMBEDDING_MODEL
HARBORRAG_EMBED_API_KEY=REPLACE_WITH_REAL_EMBEDDING_API_KEY
HARBORRAG_EMBED_SPACE=smoke-embedding-space-v1
# HARBORRAG_EMBED_EXPECTED_DIMENSIONS=1536
```

Reranking:

```dotenv
HARBOR_SMOKE_RERANK_PROVIDER=cohere
HARBOR_SMOKE_RERANK_MODEL=cohere/REPLACE_WITH_REAL_RERANK_MODEL
HARBOR_SMOKE_RERANK_API_KEY=REPLACE_WITH_REAL_RERANK_API_KEY
```

A family you leave out exits `2` when you run its script, and `run_all.py` skips it.

### Optional settings

- Ambient cloud identity instead of an API key, where the provider supports it:
  `<PREFIX>ALLOW_AMBIENT_CREDENTIALS=true`, for example
  `HARBORRAG_CHAT_ALLOW_AMBIENT_CREDENTIALS=true`.
- Provider fields, each behind the family prefix: `API_BASE`, `API_VERSION`, `DEPLOYMENT_NAME`,
  `CUSTOM_LLM_PROVIDER`, `HEADERS_JSON`, `EXTRA_LITELLM_PARAMS_JSON`, the AWS credential and role
  fields, and the Vertex project, location and credentials fields. The template spells them out.
- Request timeout for every family, in seconds (default `90`): `HARBOR_SMOKE_TIMEOUT_SECONDS=120`.

### Choose the chat backend

`HARBORRAG_CHAT_BACKEND` picks how chat reaches the provider (default `direct_sdk`):

- `direct_sdk`: LiteLLM calls the provider directly.
- `litellm_router`: requests go through Harbor's LiteLLM Router integration.
- `litellm_proxy`: requests go to a running LiteLLM Proxy, which also needs:

  ```dotenv
  HARBORRAG_CHAT_PROXY_API_BASE=https://proxy.example.com
  HARBORRAG_CHAT_PROXY_API_KEY=REPLACE_WITH_REAL_PROXY_KEY
  # HARBORRAG_CHAT_PROXY_HEADERS_JSON={}
  ```

### Quick config in the script

`chat.py`, `embed.py` and `rerank.py` each start with plain constants, such as `EMBED_MODEL` and
`BATCH`. A constant you set replaces the matching variable from the environment or the file; leave
it `None` to use the variable. The scripts are tracked files, so set models there but keep API keys
in the git-ignored dotenv file.

## Run the checks

Run one configured family first:

```bash
uv run python packages/harborrag-adapters/tests/models/smoke/chat.py
uv run python packages/harborrag-adapters/tests/models/smoke/embed.py
uv run python packages/harborrag-adapters/tests/models/smoke/rerank.py
```

Then run every configured family:

```bash
uv run python packages/harborrag-adapters/tests/models/smoke/run_all.py
```

Each request gets one attempt, with no deployment failover or model fallback, which keeps the cost
low and a failure easy to read.

## What a pass checks

| Script | Request | Passes when |
| --- | --- | --- |
| `chat.py` | A system and a user message | The answer is not empty and carries the provider model and request ID |
| `embed.py` | Two short strings in one batch | Two finite vectors come back with the same positive dimension |
| `rerank.py` | Three documents, top two requested | Two distinct in-range indexes come back with finite scores |

A passing run prints the provider model and its latency, dimension or result count. `chat.py` also
prints the answer, and `embed.py` the first five values of each vector; both inputs are fixed test
strings. No script prints credentials, headers or raw provider bodies.

`run_all.py` skips a family that exits `2`. It exits `0` when every configured family passes, `1`
when any fails, and `2` when no family is configured.

## Troubleshooting

### The script says `not configured`

- Check that the family's `PROVIDER` and `MODEL` variables are set: `HARBORRAG_CHAT_`,
  `HARBORRAG_EMBED_` or `HARBOR_SMOKE_RERANK_` followed by `PROVIDER` and `MODEL`.
- Replace every `REPLACE_WITH_REAL` placeholder.
- If the file is not the repository-root `.env`, set `HARBOR_SMOKE_ENV_FILE` to its path.
- Look for a variable already exported in your shell, even an empty-looking one: it wins over the
  file.

### An import or optional dependency fails

Run the script from the repository root with the workspace environment, and resync it as
[CONTRIBUTING](../../../../../CONTRIBUTING.md#development-setup) describes.

### Authentication, model or endpoint errors

- Check that the key is allowed to use that provider and model.
- Use the provider's LiteLLM model name.
- For an OpenAI-compatible gateway, set `API_BASE` and, when needed, `CUSTOM_LLM_PROVIDER` or
  custom headers.
- For an Azure-style deployment, check `API_BASE`, `API_VERSION` and `DEPLOYMENT_NAME` separately
  from the model name.
- For `litellm_proxy`, check the proxy URL and proxy key variables.

### The request times out or seems to hang

Check DNS, firewall, VPN, proxy and the provider's status from the same shell. LiteLLM may try to
refresh its model-cost data before the request; when that fails it warns and uses its packaged data.
The request timeout does not cover network activity during import.

### Embedding dimensions fail validation

Remove `HARBORRAG_EMBED_EXPECTED_DIMENSIONS` to see the size the model returns, or set it to the
dimension the model guarantees. Do not reuse an embedding-space name for models whose vectors are
not compatible.

## Keep credentials and output safe

- Use test credentials with the smallest permissions and quota that work.
- Never paste the dotenv file, raw headers or provider response bodies into test evidence or an
  issue.
- The scripts redact common credential formats from errors, but read captured output before you
  share it.
