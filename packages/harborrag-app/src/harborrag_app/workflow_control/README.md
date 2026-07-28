# Workflow control

## Responsibility

Translates HTTP and CLI requests into Temporal runtime-client operations and
returns one transport-neutral response envelope.

## Inputs / Outputs

Transport arguments → `AppResponse`

## Must not

- import adapter implementations
- implement ingestion business rules
- expose Temporal objects directly to HTTP or CLI callers
