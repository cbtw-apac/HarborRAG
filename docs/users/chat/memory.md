# Conversation memory

Public chat and agent requests use the latest **three completed
user/assistant exchanges** from their conversation. Older messages remain
stored for browsing; dropping them from a prompt does not erase history.

## Identity and scope

Authenticated storage and lookup are scoped by `(tenant, user_id, session_id)`.
`user_id` comes from the verified JWT claim selected by
`HARBORRAG_AUTH_USER_ID_CLAIM` (`sub` by default); the credential subject is
retained for audit. The claim must be stable across requests. A different
signed user in the same tenant cannot open the session, even with its ID.
HTTP does not accept a user ID from the request body or query. Local
`auth_mode=none` uses the shared `DEFAULT_USER` development identity.

Direct SDK callers can supply explicit user identities. CLI and
application-service calls that omit one use `DEFAULT_USER`. Tenant authorization
continues to apply. Existing sessions under the old shared identity require a
trusted owner mapping before migration; the API does not expose them to all users.

A session can alternate between the chat and agent completion endpoints.
Its stored `kind` describes how it was created, not an execution restriction.

## Which history is used

Before generation, the server reads the newest three complete exchanges,
oldest first, then appends the current question once. It excludes:

- Unanswered user messages and orphan assistant messages.
- Partial or empty answers.
- Assistant tool-call stubs and tool results.

Only prior complete exchanges enter this window. In RAG mode, retrieval
searches the raw question. Public completion requests do not run rolling
summaries, query rewriting, long-term recall, automatic extraction, or memory
tools. The configurable lower-level SDK memory facilities remain available
for explicit use; their settings do not change this public three-exchange
policy.

The model's context budget may retain fewer complete pairs. A history read
failure falls back to an empty window and logs identifiers without message
content.

## Persistence and titles

RAG chat stores the user prompt before the model call, then stores the answer
and its actual citations. Interrupted output is marked `partial: true`.
It remains visible in message history but is excluded from subsequent
completion context.

Agent runs store their question and final answer with a `run_id`.
Intermediate tool activity belongs to the run checkpoint. A failed or
cancelled run can remain resumable independently of conversation history.

`memory_persisted` reports whether the exchange was saved. A persistence
failure leaves a successful answer available and sets this field to
`false`.

Conversations begin with no title. After the first persisted exchange, the
server uses up to ten words of the prompt, capped at 80 characters. This
requires no model call. An atomic assignment prevents competing workers from
overwriting a title; a manual rename, including clearing it, always wins.

Configure durable storage with `HARBORRAG_CONTROL_DB_URL`; deployed instances
should share a PostgreSQL control database. The development in-memory
fallback is bounded and does not survive restart or coordinate replicas.

## Browse, rename, and erase

All endpoints below require tenant access and the `reader` role when
authentication is enabled.

```bash
curl "http://localhost:8000/v1/chat/sessions?tenant=DEFAULT&limit=20" \
  -H "Authorization: Bearer $TOKEN"

curl "http://localhost:8000/v1/chat/sessions/session-.../messages?tenant=DEFAULT&limit=50" \
  -H "Authorization: Bearer $TOKEN"

curl -X PATCH "http://localhost:8000/v1/chat/sessions/session-...?tenant=DEFAULT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data '{"title":"Release policy"}'
```

Session lists return a `sessions` array, newest-activity first, default to 20 rows,
and accept `limit=1..100` and opaque `cursor`. Use `/v1/agent/sessions` for
sessions created by the agent surface; its history, rename, and delete paths
have the same shape. The path determines the listing kind, not a query parameter.
Message lists are oldest first, default to 50 rows, and accept
`limit=1..200`; pass the returned `next_cursor` as `after`.
Messages include citations, timestamps, run IDs, and partial status.

Delete with `DELETE /v1/chat/sessions/{session_id}?tenant=DEFAULT`.
The operation removes the session, its title and history, its agent-run
checkpoints, its session-scoped memories, and every memory you wrote that was
extracted from that conversation (any scope), together with their index
entries. Facts other users wrote are untouched. The response reports deletion
counts and a `status`: `completed`, or `deferred` when some index deletes are
queued for automatic retry (`deferred_index_points` says how many).

Existing long-term memory can still be inspected or erased through
`/v1/memory/memories`; user-wide erasure requires `admin`. These maintenance
operations are separate from completion context.

Deleting one memory follows ownership, not visibility. Your own user and
session memories are always yours to delete. Project, tenant, and global
memories are shared: a `reader` may delete only the ones they wrote (matched
on the signed end-user claim, not the credential); `editor` and above may
delete any shared memory in their own tenant, but not a global memory, which
only its author may delete. Anyone else gets `403`.

## Right to erasure

`DELETE /v1/memory/users/{user_id}?tenant=DEFAULT` (role `admin`) erases one
end user within one tenant:

- every conversation they own, with its title, messages, legacy turns, and
  agent-run checkpoints (the control database is authoritative, so
  conversations that never produced a memory are included);
- every long-term memory stored under them, at any scope and including
  expired rows, together with its vector-index entry;
- their idempotent completion claims and stored replies, so no request key
  replays an answer;
- their model-usage rows are re-attributed to an opaque `erased-…` token, so
  tenant totals stay correct while the rows no longer name them.

Each run is recorded in the `user_erasures` table (who, when, which tenant,
counts per store, status). A `200` reports `status: completed`; a `202`
reports `status: deferred` -- everything is erased and some vector deletes are
queued for automatic retry. If any step fails (for example a conversation is
mid-answer), the response is `409` naming the erasure id and the failed steps;
nothing is reported as erased that was not, and re-running the same request
finishes the job. A conversation that is mid-answer is kept while the user's
other conversations are still deleted. The same user id in another tenant is
never touched. The response's `conversation_messages_cleared` is deprecated and
always equals `sessions`.

The `user_erasures` row keeps the erased person's `user_id` on purpose: it is
the accountability record and the handle for re-running an erasure.
Administrative and audit identities (`activity.actor`, tenant members,
`resolved_by`, `created_by`) record who operated the system and are not erased.

`DEFAULT_USER` is refused with `422`: it is the shared identity of every caller
that sends no end-user claim, so it names no single person, and erasing it
would wipe all of those callers' history at once.

Procedure: revoke the user's credentials first, then erase. An answer still
being generated for that user when the erasure runs can otherwise write new
history after it.

## Automatic extraction scope

Automatic long-term extraction and the agent `manage_memory` tool store a
fact proposed at project scope as the user's own memory unless
`HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE=true`. Project memory is shared
with every project member, so writing it from model-proposed text is opt-in.
Existing project memories are still recalled. Tool results (retrieved
documents and tool output) are never used as extraction input.

See [Chat](README.md#migration) for the `0033`/`0034` database migration
requirement and endpoint replacements.
