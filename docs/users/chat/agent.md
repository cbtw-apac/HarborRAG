# Agent

The bounded multi-turn surface at `/v1/agent/completions`, which shares
conversation storage with chat but runs tools over several steps.

Create a session, then run bounded multi-hop completions against it:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"tenant":"DEFAULT"}' \
  http://127.0.0.1:8000/v1/agent/sessions

curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "tenant": "DEFAULT",
    "session_id": "session-...",
    "prompt": "Connect the release policy to its owning service.",
    "graph_search": true,
    "max_steps": 4
  }' \
  http://127.0.0.1:8000/v1/agent/completions
```

`session_id` and `prompt` are required in the JSON body; `tenant` defaults to `DEFAULT`,
`graph_search` defaults to `false`, and `max_steps` defaults to `4` (1–8). The
`session_id` must come from `POST /v1/agent/sessions`; a chat session returns `404`. The
optional `project_id` behaves exactly as for chat (must exist in `tenant`, echoed in the
response, `404` otherwise) and is also accepted by `resume`. The optional
`model` behaves as for chat and applies to every step of the run; `resume`
does not accept it, because a run continues under the model it started with. The agent calls enabled read-only retrieval tools repeatedly, including parallel
calls in a single step. When `graph_search` is false, graph tools are removed
from the model's tool surface. When the step budget is exhausted, the model
gets one final tool-free synthesis turn. The authenticated tenant and role
requirements match `/v1/chat`. Set `"stream": true` to receive the run as
Server-Sent Events: progress frames named after the engine event
(`run.started`, ...), then exactly one terminal `result` (the JSON shape below)
or `error` frame, under the same stream deadline as chat.

A successful response has this stable shape:

```json
{
  "id": "completion-id",
  "run_id": "run-...",
  "model": "primary",
  "provider": "openai",
  "provider_model": "openai/model-name",
  "message": {"role": "assistant", "content": "..."},
  "finish_reason": "stop",
  "stop_reason": "final_answer",
  "usage": {"prompt_tokens": 42, "completion_tokens": 18, "total_tokens": 60},
  "turns": 2,
  "tool_call_count": 1,
  "tool_calls": [{"step": 1, "tool": "vector_search", "ok": true}],
  "session_id": "session-..."
}
```

## Agent memory tools

Off by default. With `HARBORRAG_MEMORY_AGENT_TOOLS=true` an agent run is also
offered its caller's long-term memory:

| Tool | Capability | What it does |
| --- | --- | --- |
| `search_memory` | `read` | Recall the caller's durable facts, preferences, and decisions -- the same ranking a chat prompt gets. Takes a query, an optional single `scope`, and a `limit` (max 10). Each result carries `memory_id`, `scope`, `memory_type`, `content`, `valid_from`, `source_session_id`, and `entity_ids`; the entity ids are graph node keys, so a recalled memory can be followed straight into the graph tools. |
| `manage_memory` | `write` | Record one durable `fact`, `preference`, or `decision` for the caller, add-only. Identical content already stored in the same scope is kept rather than duplicated. |

**The owner is bound server-side.** Neither schema has a `tenant_id`,
`user_id`, `session_id`, `project_id`, `principal_id`, `run_id`, or `owner`
property. The owner comes from the authenticated run when the tool surface is
composed, so there is nothing for a model to spoof -- and an owner field that
turns up in the arguments anyway (from a transport that skipped schema
validation) is rejected with an error rather than quietly ignored. A requested
scope the caller cannot address degrades to the narrowest one it can; it is
never widened. Scope visibility is the same rule the rest of memory uses, so
`search_memory` can never return another caller's memory.

**Tenant scope is readable, not writable.** `search_memory` may recall a
`tenant`-wide fact, but `manage_memory` accepts only `project`, `user`, and
`session`. Because a requested scope only ever narrows and every caller can
address its own tenant, an agent allowed to ask for `tenant` would always get
it -- one user's session stating a fact for the whole organization. Writing
tenant-wide memory stays a human decision through the memory admin API.

`manage_memory` declares the `write` capability and the agent loop only offers
`read` tools, so switching the flag on today makes `search_memory` available
and leaves `manage_memory` registered but inert -- it becomes reachable only
when an agent run is explicitly granted a write capability. Both tools are
withheld entirely unless the flag is on *and* a memory store and a bound owner
are present, and a call to either while they are withheld answers exactly like
an unknown tool.

`run_id` identifies this run for `POST /v1/agent/runs/{run_id}/resume` below.
`stop_reason` says why the agent stopped calling tools:

| `stop_reason` | Meaning |
| --- | --- |
| `final_answer` | The model answered without requesting further tools |
| `max_steps` | The `max_steps` budget was exhausted; the answer is the final synthesis turn |
| `timeout` | The server-owned run budget (derived from the HTTP or stream deadline) expired; the answer is the final synthesis turn |
| `repeated_tool_call` | The model repeated an identical tool call too many times |
| `token_budget_exceeded` | `HARBORRAG_API_AGENT_TOKEN_BUDGET` was exhausted |

## Resuming a run

Each agent completion returns a `run_id`. Continue a run that stopped before finishing with:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "tenant": "DEFAULT",
    "session_id": "session-...",
    "graph_search": true,
    "max_steps": 4
  }' \
  http://127.0.0.1:8000/v1/agent/runs/run-.../resume
```

`session_id` is required and must be the agent session the run belongs to; `graph_search`
and `max_steps` carry the same defaults as a fresh completion. There is no `prompt` - the
run already has its own. The response is the same `AgentCompletionResponse` shape shown
above, with the original `run_id`. A run that is not resumable (finished, or unknown to
the caller's tenant and principal) returns `404`; `500` means agent-run checkpointing is
not configured on the server.

## Concurrent requests on one session

Completions on the same session are serialized inside one API process, so two
overlapping requests (a client retry, a double-submit) cannot interleave their
"recall history, then save turn" steps: the second sees the first's turn.
There is no `Idempotency-Key` replay for completions yet - a retried request
produces a second model call and a second stored turn.

## See also

- [Chat](README.md) - the HTTP and CLI surfaces
- [Chat models](models.md) - the model catalog and per-tenant overrides
- [Conversation memory](memory.md) - what a turn remembers and for how long
- [Agent](agent.md) - the bounded multi-turn surface
- [Limits and accounting](limits.md) - deadlines, admission control, usage
