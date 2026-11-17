# Limits and accounting

Look up what bounds a chat completion (deadlines, request rates, concurrency,
one answer at a time per session), how to retry one without paying twice, and
how its token usage and cost are reported. Operators set these limits with
environment variables on the API process; a request body cannot raise them.

For the request and response contract, see [Chat](README.md).

## Deadlines

| Variable | Default | Effect |
| --- | --- | --- |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Deadline of one completion; past it the request fails with `504`. It also bounds a `stream: true` completion, whose answer is complete before the stream opens |
| `HARBORRAG_API_STREAM_TIMEOUT_SECONDS` | `600` | Lifetime of a streamed response body |

Chat retrieval always runs hybrid search, which combines dense (embedding)
and sparse (keyword) matching. Graph observation is optional and adds work only
to `classic` turns; see [Retrieval and models](README.md#retrieval-and-models).

## Request rates and concurrency

Two limiters guard `POST /v1/chat/completions`. Both answer `429 rate_limited`
when a request would exceed them.

The per-second limiter allows each principal (the authenticated credential)
`HARBORRAG_HTTP_RATE_CHAT` completions per second, `2` by default, and answers
with a `Retry-After` header. Other `/v1` routes share
`HARBORRAG_HTTP_RATE_DEFAULT`, `10` per second.

The capacity limiter counts requests per minute and requests in flight at
three scopes:

| Scope | Requests per minute | In flight |
| --- | --- | --- |
| User | `HARBORRAG_API_REQUESTS_PER_MINUTE_PER_USER` (`60`) | `HARBORRAG_API_MAX_INFLIGHT_PER_USER` (`4`) |
| Principal | `HARBORRAG_API_REQUESTS_PER_MINUTE` (`60`) | `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` (`4`) |
| Tenant | `HARBORRAG_API_REQUESTS_PER_MINUTE_PER_TENANT` (`600`) | `HARBORRAG_API_MAX_INFLIGHT_PER_TENANT` (`40`) |

- All three scopes must admit a request.
- People with distinct signed user claims get separate user allowances, even
  behind one credential; the principal and tenant limits still apply to them
  together. With `HARBORRAG_AUTH_MODE=none`, every caller shares the one
  `DEFAULT_USER` allowance.
- A rejection names the scope that refused it in `details.limit_scope`
  (`user`, `principal` or `tenant`) and the limit in `details.limit_kind`
  (`requests_per_minute` or `max_inflight`).
- A streamed response holds its place for the whole stream.

`HARBORRAG_API_TENANT_CAPACITY_OVERRIDES` gives single tenants other limits. It
takes a JSON object of tenant IDs to any of `requests_per_minute`,
`max_inflight`, `user_requests_per_minute`, `user_max_inflight`,
`tenant_requests_per_minute` and `tenant_max_inflight`:

```bash
HARBORRAG_API_TENANT_CAPACITY_OVERRIDES='{"acme":{"tenant_requests_per_minute":6000}}'
```

A key left out keeps the default. The API refuses to start on an unknown key,
a value out of range, or a tenant limit below the user or principal limit.

Both limiters keep their counts in process for development. Outside
`HARBORRAG_ENV=dev`, `HARBORRAG_API_CAPACITY_REDIS_URL` is required, so every
API replica shares the same counts.

## One answer at a time per session

Different sessions are answered concurrently within the limits above. Requests
for one session take turns for the whole turn: reading the history, generating
the answer and storing it. A completion in a session that is already answering
is refused at once with `409 session_busy` rather than queued.

With a shared SQL control database, renewable leases coordinate API replicas:
a replica that loses its lease is cancelled, and the store rejects writes from
an expired holder. Messages are ordered by an atomic sequence. The development
in-memory fallback coordinates only one process.

## Retry without duplicates

Send an `Idempotency-Key` header, 1 to 128 characters, on
`POST /v1/chat/completions` to make a retry safe; the body takes no key. A key
is scoped to the tenant and the signed user and is kept for 24 hours.

- The same key and body return the stored answer byte for byte, with no second
  model call; the `Idempotency-Replayed: true` header marks the replay.
- `stream` is left out of the comparison, so a retry may switch between JSON
  and SSE.
- The same key with a different body is `422 validation_error` on field
  `Idempotency-Key`.
- While the first request with the key is still running, a retry is
  `409 session_busy`.
- A request that fails before any model call (an unknown session, an empty
  project, a busy session, a store or retrieval outage) leaves its key free:
  retry with the same key.
- Once retrieval has found its passages, a model may already have been paid
  for. A failure from then on is final for that key, a retry with it is
  `409 invalid_state`, and a new attempt needs a new key.

Without a key, a repeated request is a new completion.

## Usage and cost

The answer's `usage` adds up every model call of the turn: the `classic` scope
check and the answer.

```json
{"mode": "classic", "reasoning": "off", "steps": 1, "prompt_tokens": 3120,
 "completion_tokens": 284, "reasoning_tokens": 0, "cost_usd": 0.0041}
```

The amounts above are illustrative. `cost_usd` takes LiteLLM's attached
`response_cost`, or `litellm.completion_cost` over the provider-reported usage
when no price is attached. It is `null` when any call of the turn has no known
price; HarborRAG keeps no pricing table of its own. See
[LiteLLM cost tracking](https://docs.litellm.ai/docs/completion/token_usage)
for how LiteLLM prices a call.

What `usage` leaves out and how calls are recorded:

- Retrieval embeddings, reranking, infrastructure and separately invoked SDK
  memory operations are not included.
- The `classic` scope check is one bounded model call, recorded in the usage
  ledger with `finish_reason=query_scope_gate` even when it refuses the
  request; a refusal's `usage` is that call alone.
- Replays and automatic session titles make no model call.
- The usage ledger records every model call, including an answer the model
  left empty. A call without a known price records a null cost rather than
  claim a complete bill. A ledger failure does not discard the answer.
- Totals keep the tenant, verified user, principal and session they belong to.

## See also

- [Chat migration notes](README.md#migration): read them before upgrading an
  existing control database.
- [Conversation memory](memory.md): what a turn remembers and how history is
  stored.
