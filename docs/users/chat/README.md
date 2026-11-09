# Chat

Ask a question about your indexed content, over HTTP or with the
`harborrag chat` command, and get an answer that cites its sources. HarborRAG
first retrieves the passages that match the question, then asks a chat model
to answer from them; each `[n]` in the answer points to one of those passages,
called a **citation**. Ask inside a **session** to keep a conversation going,
or leave the session out for a one-off question.

This page is the reference for the chat routes and the CLI command. The other
chat pages cover [which model answers](models.md),
[what a session remembers](memory.md) and
[limits, retries and cost](limits.md). AI agents and IDEs query HarborRAG
through the [MCP tools](../detailed-guides/mcp-server/README.md) instead.

## Before you start

- A running HarborRAG with at least one ingested source. The
  [Quick Start](../../getting-started/quick-start.md) gets you there. With
  nothing ingested, a question answers `409 no_indexed_content` and no model
  is called.
- A configured chat model; see [Chat models](models.md).
- The API address. On the development stack the API listens on
  `http://127.0.0.1:8000`; `scripts/deployment/dev.sh up` starts it together
  with the services it needs.

## Ask a one-off question

A one-off question searches the whole tenant and is not stored:

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --data '{"message":"Explain HarborRAG in one paragraph."}'
```

You should get a JSON answer with `answer`, `citations` and `usage`; see
[Read the answer](#read-the-answer).

The development stack runs with `HARBORRAG_AUTH_MODE=none` and needs no
credential. With authentication on, add `--header 'Authorization: Bearer <token>'`;
the route needs the `reader` role. The tenant (the isolated workspace whose
content is searched) comes from the credential, or from an
`X-Tenant-Id: <tenant>` header when the credential holds several tenants.

Each session belongs to one signed user in one tenant; see
[Identity and scope](memory.md#identity-and-scope).

## Ask in a session

Create a session with `POST /v1/chat/sessions` (see
[Manage sessions](#manage-sessions)), then send its `session_id` with each
question. The `Idempotency-Key` header makes the request safe to retry
([Retry safely](#retry-safely)):

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: 5f0c2b1e-8d4a-4c3e-9f7b-1a2b3c4d5e6f' \
  --data '{"session_id":"<session-id>","message":"Who has Go and Kubernetes experience?"}'
```

| Request field | Default | Meaning |
| --- | --- | --- |
| `message` | Required | The question, 1 to 8,000 characters, not only whitespace |
| `session_id` | Omitted | One of your sessions; leave it out for a one-off question |
| `mode` | `classic` | `classic` or `instant`; `advanced` and `auto` are `422 mode_unavailable` until v2.1.0 |
| `reasoning` | `off` | Only `off` until v2.1.0; anything else is `422 validation_error` |
| `project_ids` | Omitted | One-off questions only: retrieval reads only these projects' connections |
| `connections` | Omitted | Connection refs (`<project_id>/<connection_id>`) to narrow retrieval to |
| `filters` | Omitted | A `Filter`; see [Filter retrieval](#filter-retrieval) |
| `stream` | `false` | `true` answers with Server-Sent Events (SSE) |

The full request and response schemas are in the API's OpenAPI document at
`/openapi.json`, browsable at `/docs`.

What a turn may read:

- A session reads only its own project's connections, so `project_ids` in a
  session is `422 validation_error` on field `project_ids`.
- A connection outside that scope is `422 connection_out_of_scope`, and an
  unknown project or connection is `422 reference_not_found`.
- A session with no project, or a one-off question with no `project_ids`,
  reads the whole tenant.

## Read the answer

```json
{
  "completion_id": "msg_5d2e",
  "session_id": "ses_9a1c",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "mode": "classic",
  "answer": "Two issues cover it: DEMO-11 [1] and DEMO-12 [2].",
  "citations": [
    {
      "n": 1,
      "chunk_id": "chk_9f31",
      "document_id": "doc_4411",
      "title": "DEMO-11 Roll out the worker on Kubernetes",
      "snippet": "Deploy the worker as a Kubernetes Deployment with two replicas...",
      "url": "https://your-company.atlassian.net/browse/DEMO-11",
      "page": null
    }
  ],
  "usage": {"mode": "classic", "reasoning": "off", "steps": 1, "prompt_tokens": 3120,
            "completion_tokens": 284, "reasoning_tokens": 0, "cost_usd": 0.0041}
}
```

- `completion_id` is the `message_id` the answer is stored under in the
  session's history, which returns the same `content`, `citations` and
  `trace_id`. A one-off question has `session_id: null` and is not stored.
- `trace_id` is the request's trace ID (`X-Trace-Id`); the turn's model calls
  carry it into telemetry.
- `answer` cites sources as `[n]`, one number per bracket. Citations are
  numbered from 1 in the order the answer first cites them; every `[n]` has a
  citation and every citation is cited. A list or range the model writes, such
  as `[1, 3]` or `[2-3]`, becomes `[1][3]` or `[2][3]`; a number that names no
  retrieved passage is removed from the answer with the space before it.
- Each citation names its `chunk_id` (the passage), `document_id` and
  `title`, a `snippet` of the passage of up to about 300 characters, and `url`
  or `page` when the source has one.
- `usage` adds up every model call of the turn: the `classic` scope check and
  the answer. `steps` counts the retrieval steps, and `cost_usd` is `null` when
  any of those calls has no known price. See
  [Usage and cost](limits.md#usage-and-cost).

A session is titled automatically after its first exchange; see
[Persistence and titles](memory.md#persistence-and-titles).

## Choose a mode

| Mode | Model calls | Graph observation |
| --- | --- | --- |
| `classic` | A request-scope check, then the answer | Per `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` |
| `instant` | The answer only | Never |

Both modes retrieve with the same settings. The `classic` scope check is a
model call that allows questions about indexed knowledge, relevant source
code, and the conversation:

- An unrelated creation request, for example "write a Python snake game", gets
  an ordinary `200` answer explaining the assistant's scope, with no citations
  and no answer call; its `usage` is the scope check's.
- "Find and explain the snake implementation in our repository" remains a
  valid search.
- The check is model-based, not an authorization boundary. Malformed check
  output fails closed with `503`.

`instant` skips the check, so it never refuses that way. The CLI and SDK run
the same pipeline.

## Handle errors

| Status | `code` | When |
| --- | --- | --- |
| `404` | `not_found` | The session does not exist or is not yours |
| `409` | `session_busy` | Another answer is being written in the session, or the first request with this `Idempotency-Key` is still running |
| `409` | `no_indexed_content` | Nothing is published in the session's or the question's projects; no model is called |
| `409` | `invalid_state` | The request with this `Idempotency-Key` failed; retry with a new key |
| `422` | `validation_error`, `reference_not_found`, `mode_unavailable`, `connection_out_of_scope` | See the request fields above |
| `429` | `rate_limited` | A rate or concurrency limit refused the request; see [Request rates and concurrency](limits.md#request-rates-and-concurrency) |
| `503` | `llm_unavailable` | No chat model is configured, the provider is down, or it answered nothing; nothing is stored |
| `503` | `dependency_unavailable` | A store or retrieval is down; `details.dependency` names it when known. If the answer was generated but could not be stored, nothing is returned or stored |
| `504` | `deadline_exceeded` | The completion ran past `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS`; see [Deadlines](limits.md#deadlines) |

A completion in a busy session is refused at once rather than queued.

## Retry safely

Send an `Idempotency-Key` header, 1 to 128 characters, to make a retry safe:
within 24 hours the same key and body return the stored answer with no second
model call. When a key stays free after a failure and when it needs replacing
is in [Retry without duplicates](limits.md#retry-without-duplicates).

## Filter retrieval

`filters` takes `{"and": [...]}` of conditions `{"field", "op", "value"}`:

- `eq` or `in` on an indexed evidence field, such as `document_kind` or
  `labels`;
- `gt`, `gte`, `lt` or `lte` on a number field (`fields.<key>`); the evidence
  fields above match exactly and take no range;
- `source_scope_id` takes source scope IDs, never numbers;
- `connection` with `eq` or `in` and connection refs, which narrows like
  `connections`.

`or`, nested filters and the other operators are `422 validation_error` on
field `filters` in this release. The indexed fields and the `fields.<key>`
naming are listed in
[Filter keys for `vector_search`](../detailed-guides/mcp-server/setup-and-integration.md#filter-keys-for-vector_search).

## Stream the answer

`"stream": true` answers with `text/event-stream`. The stream is sent once the
answer is complete: `response.started` (`session_id`, `mode`, `replayed`), one
`response.output_text.delta` with the whole answer in `content`, then
`response.completed` with the same body as JSON. A request that cannot be
answered fails with an ordinary HTTP error before the stream opens.

The `response.*` event names are the 2.0 names and will change when streaming
moves to its new event contract; do not build on them beyond this release.

## Retrieval and models

Chat searches the raw question with hybrid retrieval, which combines dense
(embedding) and sparse (keyword) matching, within the connections the turn may
read. Graph-enabled `classic` turns observe the graph around the passages
found; a turn narrowed to projects or connections does not widen its results
through the graph. The prompt separates conversation context from document
evidence and tells the model to cite only the sources it uses.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_CHAT_RETRIEVAL_TOP_K` | `5` | Requested retrieval candidates; applies until `retrieval.top_k` is overridden for the session's project or the tenant ([Runtime settings](../configuration/runtime-settings.md)) |
| `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` | `false` | Graph observation in `classic` mode |
| `HARBORRAG_CHAT_RETRIEVAL_MIN_RELEVANCE` | `0.0` | Optional evidence relevance threshold, compared with the retrieval similarity, never the rerank score |

`retrieval.top_k` and `retrieval.rerank` are read on every turn, at the level of
the session's project, or of the one project a one-off question names, else the
tenant; a change applies without a restart. With `retrieval.rerank` on, chat
retrieves a pool of four times `top_k` passages (at most 100), reorders it with
the rerank model of the model catalog, which must have a `rerank` section, and
keeps the first `top_k`; see
[Rerank chat retrieval](../configuration/runtime-settings.md#rerank-chat-retrieval).
Results without a relevance score are kept. Prompt budgets can leave out whole
passages and older exchanges of the three-exchange window.

Chat answers with the `chat` family of `config/models/models.yaml`. HTTP and
the CLI use the server-owned `default` prompt; callers cannot supply prompt
paths. See [Chat models](models.md) for the catalog and per-tenant models.

## Manage sessions

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/chat/sessions` | Create a session, optionally titled and scoped to one project |
| GET | `/v1/chat/sessions` | List your chat sessions, newest first; `q` searches titles |
| GET | `/v1/chat/sessions/{session_id}/messages` | Read a session's history, newest first |

The session routes take the tenant from the credential, or from `X-Tenant-Id`
when the credential holds several. Create a session scoped to a project:

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/sessions \
  --header 'Content-Type: application/json' \
  --data '{"title": "Worker rollout questions", "project_id": "<project-id>"}'
```

The `201` response is a `Session`: `session_id` (`ses_…`), `title` (`""` when
untitled), `project_id`, `profile_id` (`null` until v2.1.0), `message_count`,
`created_at` and `updated_at`.

- `project_id` is fixed for the session's life; a project the tenant does not
  have is `422 reference_not_found`.
- `profile_id` is refused with `422 validation_error` until v2.1.0.
- Session routes answer with `Cache-Control: no-store`.
- Another reader's session is `404`, the same as an unknown one.
- Sessions created by the 1.x agent surface are listed with the rest.

Paging, title search and the message fields are in
[Browse sessions and history](memory.md#browse-sessions-and-history).
Renaming and deleting a session come back in v2.1.

## Ask from the CLI

From a repository checkout, the CLI does not read the worker's
`env/.env.temporal`, so first export the shared-corpus settings in the shell
you ask from, or retrieval finds nothing:

```bash
export HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
export HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
```

Then ask, and pass the returned `session_id` back to continue the
conversation:

```bash
uv run harborrag chat "Explain HarborRAG in one paragraph." --json
uv run harborrag chat "Explain the last point." --session <session-id> --json
```

How the command behaves:

- Without `--session`, it creates a session, in the project `--project` names
  when given (validated like HTTP). An existing session keeps its own project,
  so `--session` with `--project` is refused.
- `--tenant` picks the tenant, `DEFAULT` unless you pass another.
- It answers in `classic` mode.
- `--json` prints one envelope, `{"ok": ..., "data": ..., "error": ...}`,
  whose `data` is the same completion body as the HTTP answer. Pass its
  `session_id` to `--session` to continue the conversation.

All options are in the [CLI reference](../cli-reference/README.md).

## Migration

This is a breaking HTTP change; stored sessions and history are preserved.
[Upgrading: Chat](../upgrading.md#chat) lists every route, field and event that
changed, including the removed `/v1/agent/*` routes, `max_steps` and
`response.agent.progress`, and the new session and completion shapes. Also
replace these in clients:

- Replace `/v1/conversations` and `/v1/chat/conversations` with
  `/v1/chat/sessions`. The old routes return `404`.
- List responses use `items`, not `conversations`.
- `mode: "agent"` is removed and answers `422`.

Authenticated ownership changes from the shared `DEFAULT_USER` to the
configured signed user claim. Existing authenticated sessions stored under
`DEFAULT_USER` cannot be assigned to individual people automatically: map them
from trusted ownership records, or start fresh sessions. Do not make the old
shared namespace visible to every authenticated user.

Existing control databases must apply migrations `0033` (conversation
sequencing, title state and turn leases), `0034` (completion replay claims)
and `0047` (a session's `project_id`, an answer's `mode`, `trace_id` and
`feedback`) before serving these requests. The control-plane startup runs
migrations; check during deployment that it reaches the current schema.
