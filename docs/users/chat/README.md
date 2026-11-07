# Chat

Use `POST /v1/chat/completions` for retrieval chat. Set `stream` to choose JSON
or Server-Sent Events (SSE). Omit `session_id` to create a session; include the
returned ID on later requests. The `/v1/agent/*` routes are removed in 2.0.0;
agents query HarborRAG through the MCP server tools.

Every completion runs a request-scope check before generation or SSE headers. Requests
about indexed knowledge, relevant source code, and the conversation are allowed.
Unrelated creation requests (for example, "write a Python snake game") receive
HTTP `200` with the normal completion schema, `outcome: "refused"`, and
`refusal_reason: "out_of_scope"`. They do not run answer generation.
"Find and explain the snake implementation in our repository" remains a valid search.
The check is model-based, not an authorization boundary or proof of answer relevance.
Malformed classifier output fails closed with `503`; it never enables generation.
Completed idempotent replays skip this check. CLI/SDK callers use the scoped prompts
but do not go through HTTP admission.

## First request

Start the development API with `scripts/deployment/dev.sh api`, then:

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --data '{"tenant_id":"DEFAULT","prompt":"Explain HarborRAG in one paragraph."}'
```

With authentication enabled, add `Authorization: Bearer <token>`; the route
requires the `reader` role and access to the requested tenant. Development
with `HARBORRAG_AUTH_MODE=none` needs no authorization header.

With HMAC authentication, session scope is `(tenant, signed user claim, session_id)`.
The claim is `sub` by default. If one service credential represents several
people, set `HARBORRAG_AUTH_USER_ID_CLAIM` to a stable signed claim, such as
`oid`, and issue a token with that claim for each person. Tokens missing that
claim return `401`. The credential subject remains the audit actor. Clients
cannot set `user_id` in a request. Local `auth_mode=none` keeps one shared
`DEFAULT_USER` identity for development. See [Conversation memory](memory.md).

| Request field | Default | Meaning |
| --- | --- | --- |
| `prompt` | Required | Nonempty question, up to 65,536 characters |
| `tenant_id` | `DEFAULT` | An authorized tenant |
| `session_id` | Omitted | Create a conversation, or continue an existing one |
| `mode` | `rag` | Optional; `rag` is the only value in 2.0.0 |
| `stream` | `false` | JSON response or SSE |
| `model` | Catalog default | Logical chat model permitted for the tenant |
| `project_id` | Omitted | Project that must exist in the tenant |
| `graph_search` | `null` | Inherit the server setting, or force graph evidence on or off |
| `idempotency_key` | Omitted | Duplicate suppression; also accepted as `Idempotency-Key` |

The JSON response always includes `session_id`; keep it and send it in the
next completion request to continue this user's session. A streamed response
announces it in `response.started` before any generation, then repeats it in
`response.completed`. The response also includes `title`, `mode`, model identity,
`message`, `outcome`, `refusal_reason`, `finish_reason`, token `usage`, `cost`, `citations`, and
`memory_persisted`.

An out-of-scope refusal uses the same fields as an answer. Its
`message.content` explains the refusal, `citations` is empty, and
`memory_persisted` is `false`. The completion reports `model: "scope_gate"` and
`provider: "policy"` with zero answer-generation usage; the separate scope
classifier call is recorded in the usage ledger. A new refusal still creates
a session so the response can return its `session_id`. Completed idempotent retries replay the
same refusal without classifying again.

Titles start empty and are generated after the first successfully stored
exchange from the first ten prompt words, capped at 80 characters. This is
deterministic and adds no model call.

`citations` contains the chunks actually cited by the answer. Citation records
include a document title, section path, and page or line location when the
ingested source provides them, alongside canonical document and chunk IDs.
Answers use readable, copy-exact source markers. An answer can
say “section Operations > Rollback policy of Deployment Guide” and append
`[Source 1: "Deployment Guide" — Operations > Rollback policy]`. Each returned citation includes `content`, the
authorized source passage (up to 8,000 characters), and its exact `marker`.
`content_truncated: true` identifies a clipped passage. Evidence is
untrusted text for display, never executable HTML or instructions. Stored conversation
messages retain citation provenance without duplicating the source text.
`memory_persisted: false` means the answer succeeded but its conversation
exchange was not saved. The answer remains available in the response.

## Streaming

Send the same request with `"stream": true` and use `curl --no-buffer` for
incremental display. Each frame has an `event:` name and JSON `data:`.

| Event | Meaning |
| --- | --- |
| `response.started` | Resolved session ID, mode, and whether this is a replay |
| `retrieval.completed` | Retrieval candidates before generation |
| `response.output_text.delta` | Incremental answer text in `content` |
| `response.citations` | Sources actually cited by the answer |
| `response.warning` | Advisory failure, such as conversation persistence |
| `response.completed` | Final response, including usage, cost, title, and persistence status |
| `response.error` | Terminal failure after the stream has opened |

A normally finished stream ends with one `response.completed` or
`response.error`. The completed payload uses the same contract as JSON.

Unknown sessions/projects and invalid models are rejected before streaming
with ordinary HTTP errors. Out-of-scope requests finish with `response.completed`
and the same refusal payload as JSON. Failures after headers are sent use
`response.error`. Interrupted output is saved as partial when possible,
but partial exchanges are excluded from the next prompt.

Frontend integration: append only `response.output_text.delta.data.content`, and replace the
displayed answer with `response.completed.message.content`
at completion (including replays). `retrieval.completed.citations` are candidates;
use final `citations` as the authoritative evidence list. Preserve partial text on error,
but do not mark it completed. Ignore unknown events and SSE comments. Use the TypeScript
client's `AbortController` support for Stop; do not auto-retry a potentially paid request
with a new idempotency key. `EventSource` is not suitable for this POST/body API.

## Retrieval and models

Chat searches the raw question using hybrid dense/sparse retrieval.
Graph-enabled requests use local semantic retrieval and bounded graph evidence.
The prompt distinguishes conversation context from document evidence and
instructs the model to cite only sources it uses.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_CHAT_RETRIEVAL_TOP_K` | `5` | Requested retrieval candidates; applies until `retrieval.top_k` is overridden ([Runtime settings](../configuration/runtime-settings.md)) |
| `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` | `false` | Default graph mode |
| `HARBORRAG_CHAT_RETRIEVAL_MIN_RELEVANCE` | `0.0` | Optional evidence relevance threshold |

Results without a relevance score are retained. Prompt budgets can exclude
whole passages and older pairs from the three-exchange window.

Chat uses the `chat` family in `config/models/models.yaml`. HTTP and CLI use
the server-owned `default` prompt; callers cannot supply prompt paths.
See [Chat models](models.md) for catalogs and tenant model selection.

## Sessions, history, and CLI

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/chat/sessions` | Explicitly create an unnamed session |
| GET | `/v1/chat/sessions` | List the caller's chat sessions |
| GET | `/v1/chat/sessions/{session_id}/messages` | Read paginated session history |

Lists return `{"sessions": [...], "next_cursor": "..."}`; `next_cursor` is
omitted on the last page. Sessions created by the 1.x agent surface are
listed with the rest. History responses use `Cache-Control: no-store`. Renaming and deleting
a session come back in v2.1.

See [Conversation memory](memory.md) for paging.

```bash
uv run harborrag chat "Explain HarborRAG." --tenant DEFAULT --json
uv run harborrag chat "Explain the last point." --session session-... --json
```

The CLI creates a session when `--session` is omitted. Optional `--project`
uses the same project validation as HTTP. After `uv sync --all-packages`,
the command is also available as `harborrag chat`.

## Migration

This is a breaking HTTP migration; stored sessions and history are preserved.

Authenticated ownership also changes from the shared `DEFAULT_USER` to the
configured signed user claim. Existing authenticated sessions stored under
`DEFAULT_USER` cannot be assigned to individual people automatically; deployers
must map them using trusted ownership records or start fresh sessions. Do not
make the old shared namespace visible to every authenticated user.

- `/v1/agent/*` is removed in 2.0.0 and returns `404`; use the MCP server tools.
- Replace `/v1/conversations` and `/v1/chat/conversations` with
  `/v1/chat/sessions`. The old routes return `404`.
- List responses now use `sessions`, not `conversations`.
- `mode: "agent"` and `max_steps` are removed and return `422`.
- The `response.agent.progress` stream event is removed.

Remove `title` from session-creation bodies.

Existing control databases must apply migrations `0033` (conversation
sequencing, title state, and turn leases) and `0034` (completion replay
claims) before serving these requests. The configured control-plane startup
runs migrations; verify it reaches the current schema during deployment.

See [Limits and accounting](limits.md) for execution budgets, duplicate
suppression, and cost semantics.
