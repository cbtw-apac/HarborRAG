# Conversation memory

Look up what a chat turn remembers, who can read a chat session, how history is
stored and browsed, and how long SDK memories live. A **session** is a
conversation that keeps its history; an **exchange** is one question and its
answer. Each chat turn in a session sends the model the latest **three completed
exchanges** of that session. Older messages stay stored and can be browsed;
leaving them out of a prompt does not erase them.

For the request and response contract, see [Chat](README.md).

## Identity and scope

With authentication on (`HARBORRAG_AUTH_MODE=hmac`), sessions are stored and
looked up by `(tenant, user_id, session_id)`:

- `user_id` comes from the verified JWT claim that
  `HARBORRAG_AUTH_USER_ID_CLAIM` names, `sub` by default. The claim must stay
  the same for one person across requests. A token without that claim is
  refused with `401`.
- When one service credential acts for several people, set the variable to
  another stable signed claim, such as `oid`, and issue each person a token
  carrying it. Each person then has their own sessions.
- The credential subject is kept for the audit trail.
- Another signed user in the same tenant cannot open the session, even with
  its ID; it answers `404`, the same as an unknown session.
- HTTP never takes a user ID from the request body or query.

With `HARBORRAG_AUTH_MODE=none`, the local development default, every caller
shares one identity, `DEFAULT_USER`.

The `harborrag chat` CLI and application-service calls that pass no user also
use `DEFAULT_USER`. Direct SDK callers can pass an explicit user. Tenant
authorization applies in every case.

Sessions stored under the shared `DEFAULT_USER` identity by an authenticated
deployment before user claims were introduced need a trusted owner mapping
before they can be given to individual people; the API never shows them to
every user.

## Which history is used

Before the model is called, the server reads the newest three complete
exchanges of the session, oldest first, then adds the current question once.
It leaves out:

- Unanswered user messages and assistant messages without a question.
- Partial or empty answers.

The model's context budget may keep fewer exchanges. If history cannot be read,
the turn continues with an empty window and the log records identifiers, never
message content.

Retrieval always searches the raw question, in both modes. Chat completions do
not run rolling summaries, query rewriting, long-term recall or fact
extraction. The SDK memory facilities in
[SDK memory settings](#sdk-memory-settings) do these when you call them
yourself; their settings do not change the three-exchange window.

## Persistence and titles

A completion in a session stores the question and its answer together, in one
write, once the answer is complete. The answer is stored with its `mode`,
`trace_id` and numbered citations, under the `message_id` that the completion
returns as `completion_id`.

Nothing is stored when the turn fails: an unreachable model
(`503 llm_unavailable`) or a refused request leaves the history as it was. If
the store cannot keep an answer, the completion fails with `503` rather than
return an answer that history does not hold. A one-off question, asked without
`session_id`, is never stored.

A session starts with no title. After its first stored exchange, the server
titles it with up to ten words of the question, capped at 80 characters, with
no model call. Competing workers cannot overwrite each other's title. A
rename through the SDK, including clearing the title, always wins over the
automatic title.

Messages from 1.x agent runs read back as plain user and assistant messages:
their tool-call steps were removed on upgrade, and their empty assistant steps
are left out of history and of `message_count`.

Sessions live in the control database that `HARBORRAG_CONTROL_DB_URL` points
at. Give every API replica the same PostgreSQL control database. A development
or test composition that wires no persistent conversation store falls back to
an in-memory store that is lost on restart and is not shared between
processes.

## Browse sessions and history

List your sessions, then read one session's messages. Both calls need tenant
access and, with authentication on, the `reader` role:

```bash
curl "http://127.0.0.1:8000/v1/chat/sessions?limit=20&q=rollout" \
  --header "Authorization: Bearer $TOKEN"

curl "http://127.0.0.1:8000/v1/chat/sessions/<session-id>/messages?limit=20" \
  --header "Authorization: Bearer $TOKEN"
```

Replace `<session-id>` with a `session_id` from the first call. A credential
that holds several tenants also sends `--header "X-Tenant-Id: <tenant>"`.

Both lists answer `{"items": [...], "next_cursor": null}`:

- They return 20 rows by default and accept `limit` from 1 to 100.
- A non-null `next_cursor` is passed back as `cursor` for the next page.
- Sessions come newest activity first; `q` keeps the sessions whose title
  contains it, ignoring case.
- Messages come newest first. Each carries `message_id`, `session_id`, `role`,
  `content`, `mode`, `citations`, `trace_id`, `feedback` and `created_at`. A
  user message has `mode: null`, `citations: []` and `trace_id: null`.
- An answer cut short by a dropped stream is left out, and `message_count`
  counts the messages the history returns.

Renaming and deleting a session over HTTP, and the `/v1/memory/*` routes, are
removed in 2.0.0 and answer `404`; they return with the v2.1 session and memory
endpoints.

## SDK memory settings

These variables configure the SDK memory facade, `HarborRAG.memory.*`. They do
not change the three-exchange window that chat completions use.

| Variable | Default | Effect |
| --- | --- | --- |
| `HARBORRAG_MEMORY_ENABLED` | `true` | Off keeps a short verbatim window with no summary, recall, rewriting or extraction. |
| `HARBORRAG_MEMORY_EXTRACTION_ENABLED` | `true` | Distils durable facts from an exchange passed to the SDK `extract` call. |
| `HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE` | `false` | Lets extraction store facts at project scope, where every user of the project recalls them. Off, a project write stays in its session. One user's extraction never replaces a project memory another user wrote. |
| `HARBORRAG_MEMORY_RETENTION_DAYS_SESSION` | `90` | Expiry of session memories and rolling summaries. A summary's expiry restarts at each refresh. |
| `HARBORRAG_MEMORY_RETENTION_DAYS_USER` | `365` | Expiry of user memories. |
| `HARBORRAG_MEMORY_RETENTION_DAYS_PROJECT` | `0` | Expiry of project memories. |

Retention is applied as an expiry time when a memory is written, and `0` means
it never expires on its own. Expired memories are no longer recalled, searched
or returned, but no background job deletes them: the rows stay in the control
database until they are erased. A changed retention value applies only to
memories written afterwards.

## See also

- [Chat](README.md): the request and response contract, and the
  [migration notes](README.md#migration) for clients and databases from
  earlier releases.
- [Limits and accounting](limits.md): deadlines, capacity, retries and usage.
