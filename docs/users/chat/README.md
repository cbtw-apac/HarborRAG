# Chat

Use `POST /v1/chat/completions` for retrieval chat and
`POST /v1/agent/completions` for bounded agent runs. Set `stream` to choose JSON
or Server-Sent Events (SSE). Omit `session_id` to create a session; include the
returned ID on later requests. Both endpoints can use the same session.

Both HTTP modes run a request-scope check before generation or SSE headers. Requests
about indexed knowledge, relevant source code, and the conversation are allowed.
Unrelated creation requests (for example, "write a Python snake game") are rejected
with HTTP `422`, code `harbor_validation_error`, and `details.reason: out_of_scope`.
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
  --data '{"tenant":"DEFAULT","prompt":"Explain HarborRAG in one paragraph."}'
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
| `tenant` | `DEFAULT` | An authorized tenant |
| `session_id` | Omitted | Create a conversation, or continue an existing one |
| `mode` | Endpoint-owned | Optional fixed value: `rag` on chat, `agent` on agent; opposite values return `422` |
| `stream` | `false` | JSON response or SSE |
| `model` | Catalog default | Logical chat model permitted for the tenant |
| `project_id` | Omitted | Project that must exist in the tenant |
| `graph_search` | `null` | In RAG mode, inherit the server setting; in agent mode, graph tools default off |
| `max_steps` | `4` | Agent tool-loop limit, from 1 to 8 |
| `idempotency_key` | Omitted | Duplicate suppression; also accepted as `Idempotency-Key` |

The JSON response always includes `session_id`; keep it and send it in the
next completion request to continue this user's session. A streamed response
announces it in `response.started` before any generation, then repeats it in
`response.completed`. The response also includes `title`, `mode`, model identity,
`message`, `finish_reason`, token `usage`, `cost`, `citations`, and
`memory_persisted`. Agent results also include run and tool metadata.

Titles start empty and are generated after the first successfully stored
exchange from the first ten prompt words, capped at 80 characters. This is
deterministic and adds no model call. Manual renames take precedence.

`citations` contains the chunks actually cited by the answer. Citation records
include a document title, section path, and page or line location when the
ingested source provides them, alongside canonical document and chunk IDs.
RAG and agent answers use readable, copy-exact source markers. A RAG answer can
say “section Operations > Rollback policy of Deployment Guide” and append
`[Source 1: "Deployment Guide" — Operations > Rollback policy]`. Agent results
also return `citation_validation`. Each returned citation includes `content`, the
authorized source passage (up to 8,000 characters), and its exact `marker`.
`content_truncated: true` identifies a clipped passage. Older agent checkpoints may
have no content; absence must not be interpreted as an empty source. Evidence is
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
| `retrieval.completed` | RAG retrieval candidates before generation |
| `response.output_text.delta` | Incremental RAG answer text in `content` |
| `response.agent.progress` | Agent lifecycle and tool progress |
| `response.citations` | Sources actually cited by the answer |
| `response.warning` | Advisory failure, such as conversation persistence |
| `response.completed` | Final response, including usage, cost, title, and persistence status |
| `response.error` | Terminal failure after the stream has opened |

A normally finished stream ends with one `response.completed` or
`response.error`. The completed payload uses the same contract as JSON.
Agent mode streams progress and returns the answer at completion; it does not
currently stream intermediate model text.

Unknown sessions/projects and invalid models are rejected before streaming
with ordinary HTTP errors, as are out-of-scope requests. Failures after headers are sent use
`response.error`. Interrupted RAG output is saved as partial when possible,
but partial exchanges are excluded from the next prompt.

Frontend integration: append only `response.output_text.delta.data.content`, show agent
progress separately, and replace the displayed answer with `response.completed.message.content`
at completion (including replays). `retrieval.completed.citations` are candidates;
use final `citations` as the authoritative evidence list. Preserve partial text on error,
but do not mark it completed. Ignore unknown events and SSE comments. Use the TypeScript
client's `AbortController` support for Stop; do not auto-retry a potentially paid request
with a new idempotency key. `EventSource` is not suitable for this POST/body API.

## Retrieval and models

RAG mode searches the raw question using hybrid dense/sparse retrieval.
Graph-enabled requests use local semantic retrieval and bounded graph evidence.
The prompt distinguishes conversation context from document evidence and
instructs the model to cite only sources it uses.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_CHAT_RETRIEVAL_TOP_K` | `5` | Requested retrieval candidates |
| `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` | `false` | Default RAG graph mode |
| `HARBORRAG_CHAT_RETRIEVAL_MIN_RELEVANCE` | `0.0` | Optional evidence relevance threshold |

Results without a relevance score are retained. Prompt budgets can exclude
whole passages and older pairs from the three-exchange window.

Use RAG mode for a focused question that can be answered from the first ranked
passages. Use agent mode with `graph_search: true` when the question spans
multiple documents or stages and may need focused follow-up searches. A broad
workflow question can rank several chunks from its first matching document
before later-stage evidence; the agent can search those missing stages within
its bounded tool loop. Both modes remain subject to the same permission and
active-version checks.

Both modes use the `chat` family in `config/models.yaml`. HTTP and CLI use
the server-owned `default` prompt; callers cannot supply prompt paths.
See [Chat models](models.md) for catalogs and tenant model selection.

## Sessions, history, and CLI

Both endpoint families have the same session operations. Replace `{surface}`
with `chat` or `agent`:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/{surface}/sessions` | Explicitly create an unnamed session |
| GET | `/v1/{surface}/sessions` | List sessions created on this surface |
| GET | `/v1/{surface}/sessions/{session_id}/messages` | Read paginated session history |
| PATCH | `/v1/{surface}/sessions/{session_id}` | Rename or clear the title |
| DELETE | `/v1/{surface}/sessions/{session_id}` | Erase session history and associated memory |

Lists return `{"sessions": [...], "next_cursor": "..."}`; `next_cursor` is
omitted on the last page. Session kind records the creating surface. Using a
chat session for an agent turn does not move it to the agent list. Known IDs
can be read, renamed, or erased through either family, with the same tenant
authorization. History responses use `Cache-Control: no-store`.

See [Conversation memory](memory.md) for paging and deletion.

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

- Move agent requests from `/v1/chat/completions` to `/v1/agent/completions`.
  The request no longer needs a `mode` field.
- Replace `/v1/conversations` and `/v1/chat/conversations` with the appropriate
  `/v1/chat/sessions` or `/v1/agent/sessions` family. The old routes return `404`.
- List responses now use `sessions`, not `conversations`; choose the endpoint
  family instead of a `kind` query parameter.
- Resume through `POST /v1/agent/runs/{run_id}/resume`; `/v1/runs/*` is removed.
- Old agent SSE consumers must switch from `run.*` / `result` / `error` to the
  shared event names above. Use `response.completed` as the final authority.

Remove `title` from session-creation bodies. The separate memory-erasure and
graph-traverse aliases retain deprecation headers; chat and agent paths do not.

Existing control databases must apply migrations `0033` (conversation
sequencing, title state, and turn leases) and `0034` (completion replay
claims) before serving these requests. The configured control-plane startup
runs migrations; verify it reaches the current schema during deployment.

See [Agent](agent.md) and [Limits and accounting](limits.md) for execution
budgets, duplicate suppression, and cost semantics.
