# Ingestion dashboard

## Responsibility

Renders workflow-control status and sends explicit refresh, pause, resume, and
cancel commands from the interactive CLI. Keys: `F` refresh, `P` pause, `R` resume,
`X` cancel, `Q` quit.

## Inputs / Outputs

`AppResponse` payloads → Textual widgets and workflow-control commands

## Must not

- call adapters or repositories
- implement ingestion state transitions
- own Temporal client configuration
