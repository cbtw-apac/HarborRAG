# Agent

Agent runs a bounded model/tool loop through `POST /v1/agent/completions`.
It uses the same user-owned sessions, three-exchange history window, and
cost contract as RAG chat.

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/agent/completions \
  --header 'Content-Type: application/json' \
  --data '{
    "tenant": "DEFAULT",
    "prompt": "Connect the release policy to its owning service.",
    "graph_search": true,
    "max_steps": 4
  }'
```

Omit `session_id` for a new session, or reuse a session belonging to the same
signed user and tenant. `model` selects a permitted logical chat model for every
step. `project_id` is optional and must exist in the tenant.
Authentication and role requirements match [Chat](README.md).

## Tools and stopping

The agent and MCP server use the shared retrieval tool catalog. It includes
vector search, canonical chunk fetching, document context/metadata, source
and document listing, citation verification, and graph discovery/search.
See [MCP tools](../detailed-guides/mcp-server/README.md) for individual
contracts.

Graph execution defaults off in agent mode; enable `graph_search` to
permit graph search and composed graph evidence tools. The public agent does
not expose memory search or mutation tools.

Prefer this mode for cross-document or multi-stage questions. The agent can
start with vector search, carry a returned chunk or entity identifier into a
focused graph-capable search, and disclose a missing evidence stage instead of
silently completing the chain. Focused single-passage questions usually need
only RAG mode.

`max_steps` defaults to 4 and accepts 1–8. The model can issue parallel
read-only tool calls within a step. The engine also enforces elapsed-time,
token, and repeated-call limits. A guarded stop normally gets one final
tool-free synthesis call when the remaining budget permits.

The response adds `run_id`, `stop_reason`, `turns`, `tool_call_count`,
a bounded `tool_calls` trace, and citation validation to the shared completion
fields. Source-backed claims use copy-exact, readable markers such as:

```text
[Source: "Deployment Guide" — Operations > Rollback policy (ref 6f2a91cd17a4)]
```

The short reference suffix disambiguates passages with the same document and
section labels. Each item in `citations` carries `document_title`,
`section_path`, a page, line, or passage `location` when available, and the
canonical `document_id` and `chunk_id`. `citation_validation.complete` is
false if the answer invents a marker. `evidence_available` and `marker_count`
make an uncited answer visible to clients and release gates without treating a
safe abstention as a citation failure. Unsupported markers never become
citation records. The same validated citations are saved with the assistant's
conversation message.

Stop reasons include `final_answer`, `max_steps`, `timeout`,
`repeated_tool_call`, and `token_budget_exceeded`.
Token usage and generation cost aggregate every model call, including final
synthesis and calls saved before a resume.

With `stream: true`, the server emits `response.started`,
`response.agent.progress`, and one terminal `response.completed` or
`response.error`. Progress contains the engine event name, run ID, and safe
metadata. The final answer arrives in `response.completed`.
See the complete [SSE contract](README.md#streaming).

## Resume an interrupted run

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/agent/runs/run-.../resume \
  --header 'Content-Type: application/json' \
  --data '{"tenant":"DEFAULT","session_id":"session-...","max_steps":4}'
```

Resume requires the original session ID and returns JSON. It accepts the
usual project scope, graph switch, and step bound, but no new prompt or model.
The response uses the same completion fields as the initial request, with `mode: agent`,
source `citations[].content`, citation validation, and cumulative usage/cost. Responses
are marked `Cache-Control: no-store`. Resume continues an existing run; it does not
run a new request-scope classification.
An explicitly selected logical model is retained in the checkpoint; runs
created without one continue to use the configured default.

Cancelled runs, retryable failures, and running checkpoints whose executor
lease expired may resume. Completed or nonretryable failed runs return
`404`; an active executor lease returns `409`. Optimistic checkpoint
versions prevent two resumptions from claiming the same state.

A completed checkpoint and conversation history are separate writes. If
history persistence fails, the answer remains successful with
`memory_persisted: false`; the run checkpoint still records completion.

Use completion idempotency keys to replay an already completed request.
Resumption is for unfinished runs, not for replaying completed answers.

See [Limits and accounting](limits.md) for budgets, concurrency, and pricing.
Browse agent sessions with `GET /v1/agent/sessions` and their history with
`GET /v1/agent/sessions/{session_id}/messages`. See the
[session and migration guide](README.md#migration).
