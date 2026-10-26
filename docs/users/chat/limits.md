# Limits and accounting

Completion requests are bounded by server deadlines, capacity limits, and
conversation coordination. Cost reporting uses LiteLLM's response pricing.

## Deadlines and retrieval

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | JSON completion/resume deadline; exceeding it returns `504` |
| `HARBORRAG_API_STREAM_TIMEOUT_SECONDS` | `600` | SSE lifetime; deadline failures use `response.error` |
| `HARBORRAG_API_AGENT_TOKEN_BUDGET` | `120000` | Aggregate prompt and completion tokens across an agent run |

The agent's execution timeout reserves 30 seconds for final synthesis and
a five-second margin inside the applicable HTTP deadline. A nonpositive
derived budget is rejected. Request bodies cannot extend these server limits.

RAG retrieval always uses dense/sparse hybrid search. Graph mode is
optional; it adds graph evidence and work to retrieval. Use
`graph_search: true` or `false` to override the RAG server default.
See [retrieval settings](README.md#retrieval-and-models).

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
A busy session can return `409` when lease acquisition times out.
The development in-memory fallback coordinates only one process.

## Duplicate suppression

Supply `Idempotency-Key` or body `idempotency_key` on
`POST /v1/chat/completions`. If both are present, they must match.
Keys are scoped to tenant and logical user.

Repeat the same request and key to receive the stored completed response
without another model call. Delivery format is excluded from the request
fingerprint, so a retry may switch between JSON and SSE. For SSE replay,
the server sends `response.started` followed by `response.completed`;
it does not regenerate text deltas. `Idempotency-Replayed` indicates replay.

A key reused for a different request, an in-progress or uncertain operation,
or a failed operation returns `409`. Failed keys do not automatically
restart paid work. A deliberate new attempt needs a new key. Without a key,
a repeated request is a new completion.

## Usage and cost

The response includes token `usage` and a separate cost object:

```json
{
  "amount_usd": 0.003,
  "currency": "USD",
  "status": "estimated",
  "complete": true,
  "scope": "answer_generation",
  "model_calls": 2,
  "priced_model_calls": 2
}
```

The amount above is illustrative. The adapter uses LiteLLM's attached
`response_cost`, or `litellm.completion_cost` with provider-reported usage
when attached pricing is absent. Cached-token details are preserved.
HarborRAG does not maintain a separate handwritten pricing table here.
See [LiteLLM cost accounting](https://docs.litellm.ai/docs/completion/token_usage)
for the response fields and calculator contract.

For agent runs, amounts and token counts include every generation step and
synthesis call, including checkpointed work before resumption.
`amount_usd` is the known subtotal; `complete: false` signals unpriced
calls. With no prices available, the amount is `null` and status is
`unavailable`. An explicitly reported zero remains a valid estimate.

The scope is **answer generation**. Retrieval embeddings, reranking,
request-scope classification, infrastructure, and separately invoked memory operations
are not included. HTTP admission uses one additional bounded model call per fresh
request, recorded separately in the usage ledger with `finish_reason=query_scope_gate`,
even when it rejects the request. Completed replays and run resumptions skip admission.
Automatic conversation titles add no model call.

The usage ledger records completed chat/agent usage and reported usage from
partial RAG streams. Complete agent aggregates are recorded as their full
cost; an aggregate with missing prices records a null scalar cost rather
than claiming a complete bill. A ledger failure does not discard the answer.
Totals retain tenant, verified user, principal, session, and run attribution
where applicable.

See [Chat migration](README.md#migration) before upgrading an existing
control database, and [Conversation memory](memory.md) for persistence and
history behavior.
