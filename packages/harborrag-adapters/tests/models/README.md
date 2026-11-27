# Model adapter tests

These tests cover the shared model runtime and the chat, embedding and reranking clients.

| Folder | What it covers |
| --- | --- |
| `unit/` | Configuration, normalization, routing, lifecycle, cache, telemetry and client behaviour, with injected transports |
| `contract/` | One conformance suite for every chat client and chat backend; see [Chat contract suites](contract/chat/README.md) |
| `chaos/` | Deterministic fault injection: circuit breaking, Redis lock loss and cancellation; see [Run fault and load tests](#run-fault-and-load-tests) |
| `performance/` | Local concurrency limits and single-flight load; see [Run fault and load tests](#run-fault-and-load-tests) |
| `smoke/` | Standalone scripts that send small real chat, embedding and reranking requests |

Run the collected tests from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/models
```

The smoke scripts call real providers, so they can spend paid quota and need a model, credentials
and an endpoint for each family. Set them up with [Model smoke checks](smoke/README.md) first.

## Run fault and load tests

Both suites run locally without calling providers or running a real Redis server. Fault
tests exercise circuit recovery and verify that a failed or cancelled leader releases the
single-flight lock. Load tests check request coalescing and admission limits and are
correctness checks.

| Goal | Command |
| --- | --- |
| Run the tests marked `chaos` | `uv run pytest packages/harborrag-adapters/tests/models/chaos -m chaos` |
| Run the local load tests marked `load` | `uv run pytest packages/harborrag-adapters/tests/models/performance -m load` |
