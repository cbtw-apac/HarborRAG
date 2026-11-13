# Engine ingestion smoke test

A fast, provider-independent liveness check of the engine ingestion path. `run.py`
builds a canonical document, chunks it and creates a structural graph projection through
the public engine APIs. It loads no credentials, calls no connector or model provider,
starts no Temporal, opens no database and writes no artifacts.

## Run it

From the repository root:

```bash
uv run --package harborrag-engine python \
  packages/harborrag-engine/tests/ingestion/smoke/run.py
```

It prints a one-line JSON summary:

```text
{"chunks": 3, "graph_nodes": 7, "graph_relations": 7, "status": "PASS"}
```

A non-zero exit code means an invariant failed.

## What belongs elsewhere

Detailed chunking and projection behaviour is tested in `tests/ingestion/unit` and
`tests/ingestion/integration`. Full workflow and provider smoke checks belong to the
runtime and adapter packages that own those boundaries; the deployed ingestion smoke is
in [`harborrag-runtime`](../../../../harborrag-runtime/tests/runtime_ingestion/smoke/README.md).
