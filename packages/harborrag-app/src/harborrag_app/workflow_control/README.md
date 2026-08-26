# Workflow control

## Responsibility

Translates HTTP and CLI requests into Temporal runtime-client operations and
returns one transport-neutral response envelope.

## Package layout

- `agent/`, `chat/`, and `memory/` own interactive use cases.
- `ingestion/` owns ingestion commands, ports, presentation, recovery, and orchestration.
- `retrieval/` owns vector and graph retrieval use cases.
- `control_plane/` owns project, source, activity, settings, and metrics reads.
- `composition/` owns the application facade, resource lifecycle, and environment selection.
- `ports.py`, `schemas.py`, and `errors.py` define package-wide boundaries.

## Inputs / Outputs

Transport arguments → `AppResponse`

## Must not

- import adapter implementations
- implement ingestion business rules
- expose Temporal objects directly to HTTP or CLI callers
