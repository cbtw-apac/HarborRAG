# Chat

Use `POST /v1/chat/completions` to ask a question and get an answer with
numbered citations. Ask inside a session to keep a conversation, or leave out
`session_id` for a one-off question. The `/v1/agent/*` routes are removed in
2.0.0; agents query HarborRAG through the MCP server tools.

## First request

Start the development API with `scripts/deployment/dev.sh api`, then ask a
one-off question over the whole tenant:

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --data '{"message":"Explain HarborRAG in one paragraph."}'
```

With authentication enabled, add `Authorization: Bearer <token>`; the route
requires the `reader` role. The tenant comes from the credential, or from
`X-Tenant-Id` when it holds several. Development with `HARBORRAG_AUTH_MODE=none`
needs no authorization header.

With HMAC authentication, session scope is `(tenant, signed user claim, session_id)`.
The claim is `sub` by default. If one service credential represents several
people, set `HARBORRAG_AUTH_USER_ID_CLAIM` to a stable signed claim, such as
`oid`, and issue a token with that claim for each person. Tokens missing that
claim return `401`. The credential subject remains the audit actor. Clients
cannot set `user_id` in a request. Local `auth_mode=none` keeps one shared
`DEFAULT_USER` identity for development. See [Conversation memory](memory.md).

## Ask in a session

Create a session in a project with `POST /v1/chat/sessions` (see
[Sessions, history, and CLI](#sessions-history-and-cli)), then send its
`session_id`:

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: 5f0c2b1e-8d4a-4c3e-9f7b-1a2b3c4d5e6f' \
  --data '{"session_id":"<session-id>","message":"Who has Go and Kubernetes experience?"}'
```

| Request field | Default | Meaning |
| --- | --- | --- |
| `message` | Required | The question, 1 to 8,000 characters, not only whitespace |
| `session_id` | Omitted | A session of yours; leave it out for a one-off question |
| `mode` | `classic` | `classic` or `instant`; `advanced` and `auto` are `422 mode_unavailable` until v2.1.0 |
| `reasoning` | `off` | Only `off` until v2.1.0; anything else is `422 validation_error` |
| `project_ids` | Omitted | One-off questions only: retrieval reads only these projects' connections |
| `connections` | Omitted | Connection refs (`<project_id>/<connection_id>`) to narrow retrieval to |
| `filters` | Omitted | A `Filter`; see [Filters](#filters) |
| `stream` | `false` | `true` answers with Server-Sent Events (SSE) |

The table covers asking a question; the full request and response schemas are in
the API's OpenAPI document at `/openapi.json`, browsable at `/docs`.

A session reads only its own project's connections, so `project_ids` in a
session is `422 validation_error` on field `project_ids`. A connection outside
that project is `422 connection_out_of_scope`, and an unknown project or
connection is `422 reference_not_found`. A session with no project, or a
one-off question with no `project_ids`, reads the whole tenant.

## Answer

```json
{
  "completion_id": "msg_5d2e",
  "session_id": "ses_9a1c",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "mode": "classic",
  "answer": "Two candidates match: DEMO-11 [1] and DEMO-12 [2].",
  "citations": [
    {
      "n": 1,
      "chunk_id": "chk_9f31",
      "document_id": "doc_4411",
      "title": "DEMO-11 Senior Go engineer",
      "snippet": "Five years of Go and Kubernetes...",
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
- `trace_id` is the request's trace id (`X-Trace-Id`); the model calls of the
  turn carry it into the telemetry.
- `answer` cites sources as `[n]`, one number per bracket. Citations are
  numbered from 1 in the order the answer first cites them; every `[n]` has a
  citation and every citation is cited. A list or range the model writes, such
  as `[1, 3]` or `[2-3]`, becomes `[1][3]` or `[2][3]`; a number that names no
  retrieved passage is removed from the answer with the space before it.
- Each citation names its `chunk_id`, `document_id` and `title`, a `snippet` of
  the passage of up to about 300 characters, and `url` or `page` when the
  source has one.
- `usage` adds up every model call of the turn: the `classic` scope check
  and the answer. `steps` is the retrieval steps, and `cost_usd` is `null`
  when any of those calls has no known price. See
  [Limits and accounting](limits.md).

Titles start empty and are generated after the first stored exchange from the
first ten words of the question, capped at 80 characters. This adds no model
call.

## Modes

| Mode | Model calls | Graph observation |
| --- | --- | --- |
| `classic` | A request-scope check, then the answer | Per `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` |
| `instant` | The answer only | Never |

Both modes retrieve with the same settings. The `classic` scope check is a
model call that allows questions about indexed knowledge, relevant source
code, and the conversation. An unrelated creation request (for example,
"write a Python snake game") gets an ordinary `200` answer explaining the
assistant's scope, with no citations and no answer call; its `usage` is the
scope check's.
"Find and explain the snake implementation in our repository" remains a valid
search. The check is model-based, not an authorization boundary. Malformed
check output fails closed with `503`. `instant` skips the check, so it never
refuses that way. CLI and SDK callers run the same pipeline.

## Errors

| Status | `code` | When |
| --- | --- | --- |
| `404` | `not_found` | The session does not exist or is not yours |
| `409` | `session_busy` | Another answer is being written in the session, or the first request with this `Idempotency-Key` is still running |
| `409` | `no_indexed_content` | Nothing is published in the session's or the question's projects; no model is called |
| `409` | `invalid_state` | The request with this `Idempotency-Key` failed; retry with a new key |
| `422` | `validation_error`, `reference_not_found`, `mode_unavailable`, `connection_out_of_scope` | See the request fields above |
| `503` | `llm_unavailable` | No chat model is configured, the provider is down, or it answered nothing; nothing is stored |
| `503` | `dependency_unavailable` | A store or retrieval is down; `details.dependency` names it when known. If the answer was generated but could not be stored, nothing is returned or stored |

A completion in a busy session is refused at once rather than queued.

## Retry safely

Send an `Idempotency-Key` header (1 to 128 characters). Within 24 hours, the
same key and body return the stored answer byte for byte, with
`Idempotency-Replayed: true` and no second model call; history keeps one
exchange. The same key with a different body is `422 validation_error` on field
`Idempotency-Key`. `stream` is not part of the comparison, so a streamed answer
can be retried as JSON and the other way round. Keys are scoped to the tenant
and the signed user.

A request that fails before any model is called (an unknown session, an empty
project, a busy session, a store or retrieval outage) leaves its key free, so
retry it with the same key. Once retrieval has found its passages a model may
have been paid for: a failure from then on is final for that key, and a retry
with it is `409 invalid_state`.

## Filters

`filters` takes `{"and": [...]}` of conditions `{"field", "op", "value"}`:

- `eq` or `in` on an indexed evidence field, such as `document_kind` or
  `labels`;
- `gt`, `gte`, `lt` or `lte` on a number field (`fields.<key>`); the evidence
  fields above match exactly and take no range;
- `source_scope_id` takes source scope IDs, never numbers;
- `connection` with `eq` or `in` and connection refs, which narrows like
  `connections`.

`or`, nested filters and the other operators are `422 validation_error` on
field `filters` in this release.

## Streaming

`"stream": true` answers with `text/event-stream`. Until streaming moves to the
Story 5 events, the stream is sent once the answer is complete:
`response.started` (`session_id`, `mode`, `replayed`), one
`response.output_text.delta` with the whole answer in `content`, then
`response.completed` with the same body as JSON. A request that cannot be
answered fails with an ordinary HTTP error before the stream opens.

## Retrieval and models

Chat searches the raw question using hybrid dense/sparse retrieval, within the
connections the turn may read. Graph-enabled `classic` turns observe the graph
around the passages found; a turn narrowed to projects or connections does not
widen its results through the graph. The prompt distinguishes conversation
context from document evidence and instructs the model to cite only sources it
uses.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_CHAT_RETRIEVAL_TOP_K` | `5` | Requested retrieval candidates; applies until `retrieval.top_k` is overridden for the session's project or the tenant ([Runtime settings](../configuration/runtime-settings.md)) |
| `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` | `false` | Graph observation in `classic` mode |
| `HARBORRAG_CHAT_RETRIEVAL_MIN_RELEVANCE` | `0.0` | Optional evidence relevance threshold |

`retrieval.top_k` is read on every turn, at the level of the session's project,
or of the one project a one-off question names; a change applies without a
restart. Results without a relevance score are retained. Prompt budgets can
exclude whole passages and older pairs from the three-exchange window.

Chat uses the `chat` family in `config/models/models.yaml`. HTTP and CLI use
the server-owned `default` prompt; callers cannot supply prompt paths.
See [Chat models](models.md) for catalogs and per-tenant models.

## Sessions, history, and CLI

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/chat/sessions` | Create a session, optionally titled and scoped to one project |
| GET | `/v1/chat/sessions` | List the caller's chat sessions, newest first; `q` searches titles |
| GET | `/v1/chat/sessions/{session_id}/messages` | Read a session's history, newest first |

The session routes take the tenant from the credential, or from `X-Tenant-Id`
when the credential holds several.

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/sessions \
  --header 'Content-Type: application/json' \
  --data '{"title": "Go candidates in Hanoi", "project_id": "<project-id>"}'
```

The `201` response is a `Session`: `session_id` (`ses_…`), `title` (`""` when
untitled), `project_id`, `profile_id` (`null` until v2.1.0), `message_count`,
`created_at` and `updated_at`. `project_id` is fixed for the session's life;
one the tenant does not have is `422 reference_not_found`, and `profile_id` is
refused with `422 validation_error`.

Lists return `{"items": [...], "next_cursor": null}`; pass a non-null
`next_cursor` back as `cursor` for the next page. Sessions created by the 1.x
agent surface are listed with the rest. Session routes use
`Cache-Control: no-store`. Another reader's session is `404`, the same as an
unknown one. Renaming and deleting a session come back in v2.1.

See [Conversation memory](memory.md) for paging.

```bash
uv run harborrag chat "Explain HarborRAG." --tenant DEFAULT --json
uv run harborrag chat "Explain the last point." --session ses_... --json
```

The CLI creates a session when `--session` is omitted, in the project
`--project` names (validated like HTTP). An existing session keeps its own
project, so `--session` with `--project` is refused. The CLI answers in
`classic` mode; `--json` prints the same body as the HTTP answer. After
`uv sync --all-packages`, the command is also available as `harborrag chat`.

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
- List responses use `items`, not `conversations`.
- `mode: "agent"` and `max_steps` are removed and return `422`.
- The `response.agent.progress` stream event is removed.

Session creation takes `title` and `project_id` and answers the `Session`
shape without `greeting`; history reads newest first. A completion takes
`message` instead of `prompt` and answers `completion_id`, `session_id`,
`trace_id`, `mode`, `answer`, `citations` and `usage`. See
[Upgrading](../upgrading.md#http-api-changes) for every field that changed.

Existing control databases must apply migrations `0033` (conversation
sequencing, title state, and turn leases) and `0034` (completion replay
claims) before serving these requests. The configured control-plane startup
runs migrations; verify it reaches the current schema during deployment.

See [Limits and accounting](limits.md) for execution budgets, duplicate
suppression, and cost semantics.
