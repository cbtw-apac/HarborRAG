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
The operation removes the session and its history, associated stored
checkpoints through the repository, and session-scoped memory/index entries.
The response reports deletion counts; it is not a claim that unrelated
user/project memories were removed.

Existing long-term memory can still be inspected or erased through
`/v1/memory/memories`; user-wide erasure requires `admin`. These maintenance
operations are separate from completion context.

See [Chat](README.md#migration) for the `0033`/`0034` database migration
requirement and endpoint replacements.
