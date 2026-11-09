# Limits and accounting

What bounds a chat completion (deadlines, capacity, one answer at a time per
session), how to retry one safely, and how its token usage and cost are
reported.

## Deadlines and retrieval

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Completion deadline; exceeding it returns `504`. It also bounds a `stream: true` completion, whose answer is complete before the stream opens |
| `HARBORRAG_API_STREAM_TIMEOUT_SECONDS` | `600` | Lifetime of a streamed body |

Request bodies cannot extend these server limits.

Chat retrieval always uses dense/sparse hybrid search. Graph observation is
optional; it adds graph evidence and work to `classic` turns, never to
`instant` ones. See [retrieval settings](README.md#retrieval-and-models).

## Capacity and concurrency

| Scope | Requests per minute | In flight |
| --- | --- | --- |
| User | `HARBORRAG_API_REQUESTS_PER_MINUTE_PER_USER` (60) | `HARBORRAG_API_MAX_INFLIGHT_PER_USER` (4) |
| Principal | `HARBORRAG_API_REQUESTS_PER_MINUTE` (60) | `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` (4) |
| Tenant | `HARBORRAG_API_REQUESTS_PER_MINUTE_PER_TENANT` (600) | `HARBORRAG_API_MAX_INFLIGHT_PER_TENANT` (40) |

All three scopes must admit a request. Authenticated callers with distinct
signed user claims have separate user allowances, even behind one credential;
the credential and tenant limits still apply to them together. Local
`auth_mode=none` shares a single `DEFAULT_USER` allowance.
A rejection identifies `limit_scope` and `limit_kind`. Streamed responses
hold their capacity reservation for the full stream lifetime.

`HARBORRAG_API_TENANT_CAPACITY_OVERRIDES` accepts a JSON map of tenant IDs
to supported capacity settings. Missing fields inherit defaults; invalid keys,
ranges, or inconsistent limits fail validation.

Different sessions can execute concurrently within these limits. Requests
for one session serialize their context read, generation, and persistence.
With the shared SQL repository, renewable leases coordinate API replicas.
A worker that loses its lease is cancelled; storage fencing rejects writes
from an expired holder. Message ordering uses an atomic sequence allocation.
A completion in a session that is already answering is refused at once with
`409 session_busy`.
The development in-memory fallback coordinates only one process.

## Duplicate suppression

Send an `Idempotency-Key` header on `POST /v1/chat/completions`; the body
takes no key. Keys are scoped to tenant and signed user and kept for 24 hours.

Repeat the same request and key to receive the stored answer byte for byte,
without another model call; `Idempotency-Replayed: true` marks the replay.
`stream` is left out of the comparison, so a retry may switch between JSON and
SSE. A key reused with another body is `422 validation_error` on field
`Idempotency-Key`. While the first request with the key is still running, a
retry is `409 session_busy`. A request that fails before any model call (an
unknown session, an empty project, a busy session, a store or retrieval outage)
leaves its key reusable. Once retrieval has found its passages a model may have
been charged, so a failure from then on is `409 invalid_state` on retry and a
deliberate new attempt needs a new key. Without a key,
a repeated request is a new completion.

## Usage and cost

The answer's `usage` adds up every model call of the turn, the `classic` scope
check and the answer:

```json
{"mode": "classic", "reasoning": "off", "steps": 1, "prompt_tokens": 3120,
 "completion_tokens": 284, "reasoning_tokens": 0, "cost_usd": 0.0041}
```

The amount above is illustrative. `cost_usd` uses LiteLLM's attached
`response_cost`, or `litellm.completion_cost` with provider-reported usage
when attached pricing is absent; it is `null` when any call of the turn has no
known price.
HarborRAG does not maintain a separate handwritten pricing table here.
See [LiteLLM cost accounting](https://docs.litellm.ai/docs/completion/token_usage)
for the response fields and calculator contract.

Retrieval embeddings, reranking, infrastructure, and separately invoked memory
operations are not included. A `classic` turn's scope check is a bounded model
call, recorded in the usage ledger with `finish_reason=query_scope_gate`, even
when it refuses the request; a refusal's `usage` is that call. Replays make no
model call. Automatic conversation titles add no model call.

The usage ledger records each model call, including an answer the model left
empty. A call with missing prices records a null scalar cost rather than
claiming a complete bill. A ledger failure does not discard the answer. Totals
retain tenant, verified user, principal, and session attribution.

See [Chat migration](README.md#migration) before upgrading an existing
control database, and [Conversation memory](memory.md) for persistence and
history behavior.
