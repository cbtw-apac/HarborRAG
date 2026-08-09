# Engine ingestion smoke test

This directory contains one fast, provider-independent liveness check for the
engine ingestion path. It builds a canonical document, chunks it, and creates a
structural graph projection through public engine APIs.

Run it from the repository root:

```bash
uv run --package harborrag-engine python \
  packages/harborrag-engine/tests/ingestion/smoke/run.py
```

The command prints a small JSON summary and returns non-zero when an invariant
fails. It does not load credentials, call connectors or model providers, start
Temporal, access databases, or write artifacts.

Detailed chunking and projection behavior belongs in `tests/ingestion/unit` and
`tests/ingestion/integration`. Full workflow and provider smoke checks belong to
the runtime and adapter packages that own those boundaries.
