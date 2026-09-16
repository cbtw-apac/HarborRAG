# Chat

Use `POST /v1/chat/completions` for both retrieval chat and agent runs.
Set `mode` to `rag` (default) or `agent`, and `stream` to choose JSON or
Server-Sent Events (SSE). Omit `session_id` to create a conversation; include
the returned ID on later requests. Either mode can use the same conversation.

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

The current HTTP implementation assigns every caller the logical user
`DEFAULT_USER`. Conversation scope is `(tenant, DEFAULT_USER, session_id)`.
Authorized callers within one tenant therefore share its conversation namespace;
the authenticated principal remains the audit actor. There is no request
`user_id` field. See [Conversation memory](memory.md).

| Request field | Default | Meaning |
| --- | --- | --- |
| `prompt` | Required | Nonempty question, up to 65,536 characters |
| `tenant` | `DEFAULT` | An authorized tenant |
| `session_id` | Omitted | Create a conversation, or continue an existing one |
| `mode` | `rag` | Retrieval chat or bounded `agent` execution |
| `stream` | `false` | JSON response or SSE |
| `model` | Catalog default | Logical chat model permitted for the tenant |
| `project_id` | Omitted | Project that must exist in the tenant |
| `graph_search` | `null` | In RAG mode, inherit the server setting; in agent mode, graph tools default off |
| `max_steps` | `4` | Agent tool-loop limit, from 1 to 8 |
| `idempotency_key` | Omitted | Duplicate suppression; also accepted as `Idempotency-Key` |

The response includes `session_id`, `title`, `mode`, model identity,
`message`, `finish_reason`, token `usage`, `cost`, `citations`, and
`memory_persisted`. Agent results also include run and tool metadata.

Titles start empty and are generated after the first successfully stored
exchange from the first ten prompt words, capped at 80 characters. This is
deterministic and adds no model call. Manual renames take precedence.

`citations` contains the chunks actually cited by the RAG answer as
`[Source N]`; the same citations are saved with the assistant message.
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
with ordinary HTTP errors. Failures after headers are sent use
`response.error`. Interrupted RAG output is saved as partial when possible,
but partial exchanges are excluded from the next prompt.

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

## Conversations and CLI

The canonical conversation endpoints are:

- `POST /v1/conversations`: optionally create an unnamed conversation explicitly.
- `GET /v1/conversations`: list conversations.
- `GET /v1/conversations/{session_id}/messages`: page through stored history.
- `PATCH /v1/conversations/{session_id}`: rename or clear a title.
- `DELETE /v1/conversations/{session_id}`: erase a conversation.

See [Conversation memory](memory.md) for paging and deletion.

```bash
uv run harborrag chat "Explain HarborRAG." --tenant DEFAULT --json
uv run harborrag chat "Explain the last point." --session session-... --json
```

The CLI creates a session when `--session` is omitted. Optional `--project`
uses the same project validation as HTTP. After `uv sync --all-packages`,
the command is also available as `harborrag chat`.

## Migration

Use the canonical paths above and `POST /v1/runs/{run_id}/resume`.
The former chat/agent session paths, `/v1/agent/completions`,
`/v1/chat/conversations/*`, `/v1/agent/runs/*`,
`/v1/memory/sessions/*`, and `/v1/graph/traverse` remain deprecated aliases.
Responses include `Deprecation`, a `Link` with `rel="successor-version"`,
and `Sunset: Sun, 07 Feb 2027 00:00:00 GMT`.

Remove `title` from session-creation bodies. Update SSE consumers to the
event names above; use `response.completed` as the final authority.

Existing control databases must apply migrations `0033` (conversation
sequencing, title state, and turn leases) and `0034` (completion replay
claims) before serving these requests. The configured control-plane startup
runs migrations; verify it reaches the current schema during deployment.

See [Agent](agent.md) and [Limits and accounting](limits.md) for execution
budgets, duplicate suppression, and cost semantics.
