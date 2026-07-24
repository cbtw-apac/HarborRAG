# Ingestion dashboard

## Responsibility

Renders workflow-control status and sends explicit pause, resume, retry, and
cancel commands from the interactive CLI.

## Inputs / Outputs

`AppResponse` payloads → Textual widgets and workflow-control commands

## Must not

- call adapters or repositories
- implement ingestion state transitions
- own Temporal client configuration
