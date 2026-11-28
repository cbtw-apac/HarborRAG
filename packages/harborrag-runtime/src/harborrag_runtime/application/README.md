# Application services

Find where the application services behind the HTTP API and the CLI live, and what they
must not do. The `harborrag_runtime.application` package turns a transport's arguments into
one use case (chat, memory, ingestion or a control-plane read or write) and
returns one transport-neutral `AppResponse` envelope. Durable ingestion reaches Temporal
through the runtime client; nothing in this package hands a Temporal object to a caller.

The transports live in the `harborrag-app` package: `api/` for HTTP, `cli/` for the command
line and `operations/` for the port both call. They import this package; this package never
imports an app or MCP-server module.

## Package layout

| Path | Owns |
| --- | --- |
| `chat/` | Grounded chat completions: scope, retrieval, prompting, citations and the answer events a JSON or streamed response is built from; the completion request's mode and filter refusals; `Idempotency-Key` replay |
| `memory/` | Conversation sessions, the last three exchanges a chat turn replays, the configured memory plugins, and the service methods that forward to the conversation directory and memory administration; messages, titles, erasure, the conversation directory, administration, usage records, turn locks and presenters live in `harborrag-memory`, and `MemoryIdentity` in `harborrag_core.domain.identity` |
| `retrieval/` | The corpus access mode that chat retrieval reads |
| `ingestion/` | Ingestion ports, recovery, run-progress events and inline execution; presentation and the failure inbox live in `harborrag_engine.ingestion`; starting a run (`starter.py`) and following its events (`follower.py`); durable submission goes through the ingestion application service |
| `scheduling/` | Reconciling the declared schedules of `config/sources/schedules.yaml` into the schedule engine |
| `control_plane/` | Projects, connections, active runs, runtime settings (the level and role rules, the PATCH refusals, import and export), the schedule use cases behind `/v1/admin/schedules` and the slots they fire (`schedules.py`, with the timetable rules, views and models beside it), tenant data checks, and recovery of control-plane side effects |
| `composition/` | The `AppService` facade, its resource lifecycle, service selection, the `DestinationPolicy` it hands to connection checks, and the schedule slot worker the API runs (`run_schedule_slots`) |
| `ports.py`, `schemas.py`, `errors.py` | `BaseAppService`, `AppResponse` and secret-safe error envelopes, shared by the whole package |

## Inputs and outputs

Transport arguments go in; an `AppResponse` comes out. `get_app_service()` and
`select_app_service()` in `composition/selection.py` build the service from
`RuntimeSettings` through the runtime's production composition.

## Must not

- import an app or MCP-server module;
- import adapter implementations, apart from the `DestinationPolicy` type that
  composition, connection tests and connection writes receive from the runtime;
- implement ingestion business rules;
- leave a use case's sequencing, existence checks or error translation to a transport:
  an operation calls one service method and maps its result;
- expose Temporal objects to HTTP or CLI callers.
