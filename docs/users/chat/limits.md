# Limits and accounting

The server-owned budgets a chat or agent request runs under, and the
record it leaves behind.

## Configure deadlines and budgets

The API process owns every time and token budget; callers cannot extend them.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Deadline for one non-streaming request (JSON completions and `resume`). Exceeding it returns `504`. |
| `HARBORRAG_API_STREAM_TIMEOUT_SECONDS` | `600` | Wall-clock budget for one `stream: true` body. Exceeding it ends the stream with a terminal `error` frame (`harbor_deadline_exceeded`), never a truncated body. |
| `HARBORRAG_API_AGENT_TOKEN_BUDGET` | `120000` | Total prompt+completion tokens one agent run may spend across all of its steps. |

The agent's own run timeout is derived from whichever deadline applies
(request timeout for JSON, stream timeout for SSE) minus the engine's tool-free
synthesis window (30s) and a small margin, so a run that hits its budget stops
with `stop_reason: "timeout"` and still returns an answer inside the HTTP
deadline. The configuration is rejected at request time if that derived budget
is not positive.

**Current limitation:** the retrieval engine only surfaces `HARBORRAG_CHAT_RETRIEVAL_GRAPH_SEARCH`'s
graph traversal as diagnostics/telemetry (`RetrievalDiagnostics.graph_nodes` /
`graph_relations`) - it does not yet add graph-discovered content to the
chunks used to ground the answer. Enabling the flag runs the extra graph
query (added latency, no functional effect on the answer's context yet).
Making graph search actually expand the retrieved context is a retrieval-engine
change, not a chat-layer one.

Retrieval always runs hybrid (dense + sparse) vector search; graph search is
strictly additive on top of it. Graph search adds latency, so it defaults to
off. HTTP callers can override the deployment default for one request with
`graph_search: true` or `graph_search: false`.


## Admission control

Every request must satisfy three limits, not one: the end user's, the
credential's, and the tenant's aggregate. A shared service credential
therefore no longer lets one person consume everyone else's allowance, and a
tenant with many credentials now has a ceiling.

| Scope | Requests per minute | In flight |
| --- | --- | --- |
| Per user | `HARBORRAG_API_REQUESTS_PER_MINUTE_PER_USER` (60) | `HARBORRAG_API_MAX_INFLIGHT_PER_USER` (4) |
| Per credential | `HARBORRAG_API_REQUESTS_PER_MINUTE` (60) | `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` (4) |
| Per tenant | `HARBORRAG_API_REQUESTS_PER_MINUTE_PER_TENANT` (600) | `HARBORRAG_API_MAX_INFLIGHT_PER_TENANT` (40) |

`HARBORRAG_API_TENANT_CAPACITY_OVERRIDES` takes a JSON object mapping a tenant
id to any subset of those limits. Unset fields inherit the defaults above, an
unknown tenant falls back to them, and an unknown key or out-of-range value
fails startup with the offending tenant named. A tenant aggregate below that
tenant's own per-user share is rejected the same way.

A rejection carries `limit_scope` (`user`, `principal`, or `tenant`) and
`limit_kind` (`requests_per_minute` or `max_inflight`) in the error envelope,
so an operator can tell which ceiling bound without exposing another tenant's
configuration. When the user and credential limits are equal, which is the
default, a rejection at that shared ceiling reports the `user` scope.

Reservation is all or nothing across the three scopes, so a request rejected
by the tenant aggregate leaves no counter incremented for the user or the
credential. A streamed response holds its slot for the whole body, while the
handler deadline stays scoped to the handler so it cannot cut the stream.

A credential valid for several tenants, or a wildcard one, shares a single
aggregate pool rather than one pool per tenant it names.


## Usage accounting

Every finished chat and agent turn records one usage row: the tenant, the end
user, the principal that acted, the session and run, which surface asked, the
logical model requested against the provider model that served it, the prompt
and completion token counts, the estimated cost, and the finish reason. A
partial streamed answer is recorded too whenever the provider reported any
usage, because those tokens were paid for. Totals are readable per tenant and
per user, which is what makes chat spend attributable to a human rather than
to a shared credential.

Recording is best effort in one direction only: a turn is never failed by an
accounting write. A deployment with no usage ledger wired still answers, logs
a warning outside development, and simply leaves no trail.

Chat requests also carry the end user in their model-request metadata, so
the model telemetry sinks (OpenTelemetry, and Langfuse where enabled) see the
same attribution the ledger does.

## See also

- [Chat](README.md) - the HTTP and CLI surfaces
- [Chat models](models.md) - the model catalog and per-tenant overrides
- [Conversation memory](memory.md) - what a turn remembers and for how long
- [Agent](agent.md) - the bounded multi-turn surface
- [Limits and accounting](limits.md) - deadlines, admission control, usage
