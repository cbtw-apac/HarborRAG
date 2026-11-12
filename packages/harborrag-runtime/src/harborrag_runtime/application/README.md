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
| `chat/` | Grounded chat turns: scope, retrieval, prompting, citations and streaming results |
| `memory/` | Conversation sessions, messages, titles, usage and erasure |
| `retrieval/` | The corpus access mode that chat retrieval reads |
| `ingestion/` | Ingestion commands, ports, presentation, recovery, the failure inbox and inline execution; durable submission goes through the ingestion application service |
| `scheduling/` | Reconciling `config/sources/schedules.yaml` into the schedule engine |
| `control_plane/` | Projects, connections, active runs, runtime settings import and export, and recovery of control-plane side effects |
| `composition/` | The `AppService` facade, its resource lifecycle and service selection |
| `ports.py`, `schemas.py`, `errors.py` | `BaseAppService`, `AppResponse` and secret-safe error envelopes, shared by the whole package |

## Inputs and outputs

Transport arguments go in; an `AppResponse` comes out. `get_app_service()` and
`select_app_service()` in `composition/selection.py` build the service from
`RuntimeSettings` through the runtime's production composition.

## Must not

- import an app or MCP-server module;
- import adapter implementations;
- implement ingestion business rules;
- expose Temporal objects to HTTP or CLI callers.
