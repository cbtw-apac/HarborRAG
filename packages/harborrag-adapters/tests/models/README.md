# Model adapter tests

This module owns the shared model runtime and the chat, embedding, and
reranking client families.

| Type | Scope |
| --- | --- |
| `unit/` | Configuration, normalization, routing, lifecycle, cache, telemetry, and client behavior with injected transports |
| `contract/` | Reusable chat backend conformance harness |
| `chaos/` | Deterministic provider, Redis-lock, circuit, and cancellation fault injection |
| `performance/` | Correctness-oriented local concurrency and single-flight load |
| `smoke/` | Small real chat, embedding, and reranking provider requests |

Run deterministic model coverage with:

```bash
python -m pytest packages/harborrag-adapters/tests/models
```

Live requests can consume paid quota and need provider-specific models,
credentials, endpoints, and optional LiteLLM configuration. Follow the
complete [model smoke setup](smoke/README.md) first.
