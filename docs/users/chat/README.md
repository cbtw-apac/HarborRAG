# Chat

The HTTP API and CLI chat surfaces are retrieval-grounded: every call searches
indexed HarborRAG content for the given prompt, injects the retrieved chunks
as context, and asks the model to answer from that context. Both surfaces
load the `chat` family from `config/models.yaml` and call
`AsyncHarborChatClient` through the runtime facade.

| Surface | Entry point | Best for |
| --- | --- | --- |
| HTTP API | `POST /v1/chat/sessions`, then `POST /v1/chat/completions` | Applications and authenticated services |
| HTTP API (streaming) | `POST /v1/chat/completions` with `"stream": true` | Incremental rendering as the model responds |
| HTTP API | `POST /v1/agent/sessions`, then `POST /v1/agent/completions` | Bounded multi-hop reasoning over retrieval tools |
| HTTP API | `POST /v1/agent/runs/{run_id}/resume` | Continue a paused agent run |
| CLI | `harborrag chat MESSAGE` | One-shot operator requests and scripts |

Chat and agent are not exposed as MCP tools; the retrieval tools (`vector_search`,
`graph_triplet_search`, ...) are. See [MCP Tools](../detailed-guides/mcp-server/README.md).

Chat and agent HTTP clients first create a session, then identify every
completion with only that `session_id`. Completed turns are stored in the
configured PostgreSQL control database; the latest two turns are added to each
prompt. Memory is not ingested into the RAG index.

A session is bound to the surface that created it: `POST /v1/chat/sessions`
creates a `chat` session and `POST /v1/agent/sessions` an `agent` session.
Using a chat session with `/v1/agent/completions` (or the reverse) returns
`404`, exactly like an unknown session, so chat and agent turns never
interleave in one history.

## Where to look next

This page covers the request and response contract. The rest of the
chat surface has its own pages:

| Page | Covers |
| --- | --- |
| [Chat models](models.md) | Choosing a model, per-tenant catalogs |
| [Conversation memory](memory.md) | History, recall, retention, erasure |
| [Agent](agent.md) | The bounded multi-turn surface |
| [Limits and accounting](limits.md) | Deadlines, admission control, usage |

## Configure retrieval

Two `HARBORRAG_`-prefixed runtime settings control how chat retrieves context
for every HTTP and CLI call:

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_CHAT_RETRIEVAL_TOP_K` | `5` | Number of chunks retrieved as context per call |
| `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH` | `false` | When `true`, also runs graph search (FalkorDB traversal) alongside vector search |

## Server-owned prompts

The runtime packages two Markdown system prompts. Both route the answer to
whichever source actually holds it: **this conversation** for questions about
the user or about what has already been said, and the **retrieved sources**
for questions about the indexed material, cited as `[Source N]`. The model is
told to say so plainly only when *neither* has the answer.

That distinction matters because chat retrieval has no relevance gate -- the
hybrid lane fuses by rank, so its scores say nothing about whether a match is
any good. Retrieval therefore returns its best matches even when nothing
indexed is about the question, and the prompt is what tells the model to
treat an unrelated source as absent rather than let it steer the answer. It
is also why a question the conversation has already answered -- your own name,
said a turn earlier -- is answered from the conversation rather than refused
because no document happened to contain it.

| Name | Purpose |
| --- | --- |
| `default` | General HarborRAG assistant behavior |
| `concise` | Short, direct answers |

HTTP and CLI use `default`.

Prompt names are a controlled public enum; callers cannot provide filesystem
paths or replace the stored catalog. The templates live under
`packages/harborrag-runtime/src/harborrag_runtime/chat/prompts/templates/`.

## HTTP API

Start the development API, then create a persisted session:

```bash
scripts/deployment/dev.sh api

curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"tenant":"DEFAULT"}' \
  http://127.0.0.1:8000/v1/chat/sessions
```

The `201` response contains `{"session_id":"session-...","greeting":"..."}`.
Use that ID for a completion:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "tenant": "DEFAULT",
    "session_id": "session-...",
    "prompt": "Explain HarborRAG in one paragraph."
  }' \
  http://127.0.0.1:8000/v1/chat/completions
```

The route requires the `reader` role when API authentication is enabled. Add
`Authorization: Bearer <token>` in that mode. The local development template
uses `HARBORRAG_AUTH_MODE=none` and therefore needs no header.

The JSON body requires `session_id` and `prompt`. `tenant` defaults to `DEFAULT` and
`stream` defaults to `false`. `graph_search` defaults to `null`, **not** `false`: when it
is omitted the server falls back to `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH`, so pass an
explicit `true`/`false` if you need to override the deployment setting. The HTTP service
always uses its server-owned default system prompt. Unknown sessions, or sessions owned by
another tenant or authenticated principal, return `404`.

`project_id` is optional. When present it must name a project that exists in
`tenant` (see `GET /v1/projects`); an unknown project, or one belonging to
another tenant, returns `404` exactly like an unknown session, and nothing is
remembered for that request. The turn is then stored with that project scope
and the response echoes it as `project_id` (absent when none was supplied).

`model` is optional and names one logical model from whichever catalog bounds
this tenant; a name outside it returns `422`. See
[Per-tenant models](models.md#per-tenant-models).

A successful response has this stable shape:

```json
{
  "id": "completion-id",
  "model": "primary",
  "provider": "openai",
  "provider_model": "openai/model-name",
  "message": {"role": "assistant", "content": "..."},
  "finish_reason": "stop",
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 18,
    "total_tokens": 60
  },
  "retry_count": 0,
  "fallback_count": 0,
  "session_id": "session-...",
  "project_id": "proj-...",
  "citations": [
    {"document_id": "document:...", "chunk_id": "chunk:...", "score": 0.83}
  ],
  "memory_persisted": true
}
```

`citations` lists the retrieved chunks used as context, ranked by the
retrieval engine, so callers can verify or display sources. It is empty when
retrieval finds nothing relevant.

Each source reaches the model labelled with its document title and heading
trail, not just an opaque id:

```
[Source 1] "BE Onboarding checklist" (Onboarding > Accounts) (document_id=...)
```

That is what keeps near-identical pages apart. Two Confluence checklists --
one for frontend, one for backend -- were previously indistinguishable to
the model and got merged into a single incoherent answer; with titles it
attributes each claim to the page it came from, or says which of the two it
is answering for. A chunk ingested before titles were surfaced still renders
with its id alone.

`citations` are the sources the answer **actually cited**, not everything
retrieval returned. Retrieval always hands back its top `k` whether or not
any of it bears on the question, so an answer drawn from the conversation --
"your name is Huy" -- reports no citations at all, rather than five unrelated
documents it never used. The model is instructed to mark what it uses as
`[Source N]`, and the marked sources are what appear here.

`memory_persisted` is `false` when the answer was generated but the turn could
not be saved to conversation memory (for example the control database was
briefly unavailable). Either half counts: a question that was not written
leaves the answer standing alone in history, which is as lost as no record at
all. The answer is still returned with `200`; only the next prompt's recalled
history is affected. The failure is logged server-side with tenant and session
identifiers, never with prompt or answer text.

### Streaming

Set `"stream": true` in the `POST /v1/chat/completions` body to receive Server-Sent
Events instead of one JSON object:

```bash
curl --no-buffer \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "session_id": "session-...",
    "prompt": "Explain HarborRAG in one paragraph.",
    "stream": true
  }' \
  http://127.0.0.1:8000/v1/chat/completions
```

The stream emits, in order: one `citations` event, then one or more model
event frames (`text_delta`, `reasoning_delta`, `usage`, `completed`, ...,
mirroring the underlying provider stream), and ends either after `completed`
(optionally followed by one non-terminal `warning` frame with
`code: "conversation_memory_unavailable"`, the streaming counterpart of
`memory_persisted: false`) or with exactly one terminal `error` event. Each
frame is `event: <name>\ndata: <json>\n\n`.

A completed answer is followed by one `cited_sources` event carrying the
subset the answer actually cited -- the same meaning the JSON `citations`
field has. The opening `citations` event keeps its own meaning (everything
retrieval found), because it is sent before any text exists and cannot know
what will be used; the two are separate event names so a client that appends
rather than overwrites cannot confuse them.

An unusable `session_id`, `project_id` or `model` is rejected before the
stream opens, as a `404` or `422` with the ordinary error envelope - never as
a `200` whose body then reports the problem.

A tenant that has not ingested anything yet gets `409`
(`harbor_no_indexed_content_error`) rather than a `503`: the service is
healthy and the request is well-formed, so this is something you resolve by
ingesting, not by retrying.

Past that point the response status is always `200 text/event-stream`: once
the stream starts, HTTP status can no longer change, so failures - including a
prepare-time failure such as an unreachable retrieval or chat backend -
surface as the in-band `error` event rather than a `503`. The frame names
which kind of failure it was:

| `code` | Meaning |
| --- | --- |
| `no_indexed_content` | Nothing has been ingested yet, so there is nothing to search. |
| `chat_stream_error` | The chat provider's own stream failed. |
| `harbor_not_found_error` | The session or project stopped being reachable mid-turn. |
| `harbor_validation_error` | The request was rejected once the turn was under way. |
| `harbor_deadline_exceeded` | The stream outlived `HARBORRAG_API_STREAM_TIMEOUT_SECONDS`. |
| `harbor_connection_error` | Anything else; treat it as a transient backend failure. |

The stream never ends without a `completed` or `error` frame.

An interrupted stream is not thrown away. The prompt is written to
conversation memory before the model is called, and whatever answer text
reached you is written when the stream ends however it ends - after
`completed`, after a provider failure, after the server deadline, or after you
disconnect. An answer that stopped early is flagged as partial, so a reader
of the turn can tell it was never finished, and the next turn in the session
sees the exchange instead of a gap. You are billed for those tokens either
way, so they are recorded either way.

### Manage conversations

Four routes cover the conversation resource itself: list your own
conversations, read one back, rename it, and delete it. Every one of them
derives the owner from the verified token (the authenticated subject plus the
claim named by `HARBORRAG_AUTH_USER_ID_CLAIM`), never from a request field, so
no query parameter or body key can point a listing at somebody else's
history. Listing is scoped to the authenticated **human**, which is what keeps
one shared service credential from pooling several people's conversations into
one list. All four require the `reader` role, and `tenant` defaults to
`DEFAULT` and must be one the token may access.

Name a conversation when you create it (optional, and unchanged for callers
that omit it):

```bash
curl -s -X POST http://localhost:8000/v1/chat/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"tenant":"DEFAULT","title":"Runbook triage"}'
```

A blank title leaves the conversation unnamed, and an over-long one is
truncated rather than rejected.

List your conversations, newest activity first:

```bash
curl -s "http://localhost:8000/v1/chat/conversations?tenant=DEFAULT&limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "conversations": [
    {
      "session_id": "session-...",
      "kind": "chat",
      "title": "Runbook triage",
      "created_at": "2026-01-05T09:12:44+00:00",
      "updated_at": "2026-01-05T09:31:02+00:00",
      "message_count": 6
    }
  ],
  "next_cursor": "c2Vzc2lvbi0uLi4"
}
```

`kind` is `chat` or `agent`, and passing `kind=chat` or `kind=agent` restricts
the listing to one surface. `limit` is `1..100` and defaults to `20`.

Read one conversation back, oldest message first:

```bash
curl -s "http://localhost:8000/v1/chat/conversations/session-.../messages?limit=50" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "messages": [
    {
      "message_id": "msg-...",
      "role": "user",
      "content": "Where is the runbook?",
      "created_at": "2026-01-05T09:12:44+00:00",
      "partial": false
    },
    {
      "message_id": "msg-...",
      "role": "assistant",
      "content": "In the platform wiki.",
      "created_at": "2026-01-05T09:12:46+00:00",
      "token_count": 18,
      "citations": [
        {"document_id": "document:...", "chunk_id": "chunk:...", "score": 0.83}
      ],
      "run_id": "run-...",
      "partial": false
    }
  ],
  "next_cursor": "msg-..."
}
```

`limit` here is `1..200` and defaults to `50`. `partial` is `true` when the
answer is only what a stream delivered before it ended (see
[Streaming](#streaming)); `run_id` is present on messages an agent run wrote.
Fields with no value are omitted rather than sent as `null`. An unknown
conversation, or one belonging to another person, returns the same `404` an
unknown session returns on `POST /v1/chat/completions`.

Rename a conversation (an empty or whitespace-only title clears the name):

```bash
curl -s -X PATCH "http://localhost:8000/v1/chat/conversations/session-...?tenant=DEFAULT" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"title":"Runbook triage"}'
```

```json
{"session_id": "session-...", "title": "Runbook triage"}
```

Delete a conversation:

```bash
curl -s -X DELETE "http://localhost:8000/v1/chat/conversations/session-...?tenant=DEFAULT" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "session_id": "session-...",
  "memories": 3,
  "index_points": 3,
  "sessions": 1,
  "conversation_messages_cleared": 1,
  "agent_run_checkpoints": 0
}
```

Deleting is the same erasure as
`DELETE /v1/memory/sessions/{session_id}` (see
[Inspect and erase memory](memory.md#inspect-and-erase-memory)), so it removes more
than the messages: the conversation's session-scoped memories and their vector
points go too, and the response reports what was removed. Nothing extracted
from a deleted conversation is left behind to be recalled on a later turn.

One limit is worth knowing: the erasure empties the conversation but does not
yet remove the conversation record itself, because no storage port offers an
owner-scoped conversation delete. An erased conversation therefore still
appears in `GET /v1/chat/conversations` with `message_count` of `0` until that
lands. Its content, memories, and vectors are gone.

**Paging.** Both listings are cursor-paged, and both cursors are opaque: pass
back the `next_cursor` a page returned, unchanged, to get the next page. Do
not construct or decode one, and do not reuse a cursor across a different
`limit`, `kind`, or tenant. `next_cursor` is absent on the last page, which is
how you know to stop. A cursor that is malformed, or that names a
conversation or message you do not own, is rejected with `422` rather than
silently paging from the start.

## CLI

```bash
harborrag chat \
  "Explain HarborRAG in one paragraph." \
  --tenant DEFAULT \
  --project proj-... \
  --json
```

`--project` is optional and follows the HTTP `project_id` rules: the project
must exist in the tenant, otherwise the command fails with the same not-found
error as an unknown `--session`.

From a source checkout, prefix the command with `uv run` — after
`uv sync --all-packages` the `harborrag` script is in the workspace environment:

```bash
uv run harborrag chat "Explain HarborRAG in one paragraph." --json
```

Use `--json` for the stable machine-readable command envelope, which includes
the generated `session_id` and same `citations` field as the HTTP response.

## Data and error behavior

Every transport marks chat and agent requests as sensitive, disabling
model-response caching unless a separately reviewed model policy explicitly
allows it. Raw prompts and model output are excluded from HarborRAG
application logs.

Public transports expose normalized errors. Provider exceptions and secrets
remain server-side. A `503` or streamed `error` from `/v1/chat/completions`
or `/v1/agent/completions` usually means the model configuration,
credentials, provider reachability, retrieval backend, or provider context
limit must be checked in server logs.
