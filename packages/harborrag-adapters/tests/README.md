# Adapter test suite

Tests are grouped by the production module that owns the behavior and then by
test type. This keeps every module's deterministic, resilience, performance,
and live-system coverage together.

```text
tests/
  adapters/
    unit/
    README.md
  chunking/
    unit/
    README.md
  connectors/
    unit/  failure/  security/  performance/  smoke/
    README.md
  models/
    unit/  contract/  chaos/  performance/  smoke/
    README.md
  parsers/
    unit/  failure/  security/  performance/  smoke/
    README.md
  repositories/
    unit/  integration/  smoke/
    README.md
  conftest.py
  harbor_test_builders.py
```

## Module guides

| Module | Coverage |
| --- | --- |
| [Adapters](adapters/README.md) | Package builder and top-level registries |
| [Chunking](chunking/README.md) | Splitters, refiners, and chunker registration |
| [Connectors](connectors/README.md) | Provider connectors, HTTP behavior, attachments, and live source checks |
| [Models](models/README.md) | Shared runtime plus chat, embedding, reranking, contracts, and fault injection |
| [Parsers](parsers/README.md) | Format routing, extraction, hardening, scale, and real-document checks |
| [Repositories](repositories/README.md) | Storage providers, control-plane persistence, and live backend checks |

## Test types

- `unit`: deterministic behavior using fakes or local in-process dependencies.
- `integration`: composed boundaries that exercise multiple real local layers.
- `contract`: reusable behavioral conformance across implementations.
- `failure`, `security`, and `chaos`: focused resilience and hardening checks.
- `performance`: bounded correctness-oriented concurrency, scale, and load checks.
- `smoke`: manual operations against real services, providers, or documents.

The pytest-based suites must remain deterministic unless they are explicitly
marked `integration`. A fake provider client belongs in `unit`, not `smoke`.

## Commands

Run the complete adapter suite from the repository root:

```bash
python -m pytest packages/harborrag-adapters/tests \
  --cov=packages/harborrag-adapters/src/harborrag_adapters \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=90
```

This is the adapter QA gate: all deterministic tests must pass and combined
statement/branch coverage must remain at or above 90%. Provider SDKs and live
services are replaced with fakes in unit tests; only integration and smoke
checks may depend on installed provider extras or external infrastructure.

Run a module or one test type:

```bash
python -m pytest packages/harborrag-adapters/tests/connectors
python -m pytest packages/harborrag-adapters/tests/models/unit
python -m pytest packages/harborrag-adapters/tests/models/contract -m contract
python -m pytest packages/harborrag-adapters/tests/models/chaos -m chaos
python -m pytest packages/harborrag-adapters/tests/parsers/performance -m slow
python -m pytest packages/harborrag-adapters/tests/repositories/integration -m integration
```

Behavior markers describe the observation boundary when useful:

- `blackbox`: public API behavior only.
- `graybox`: public behavior plus observable logs, routes, or injected clients.
- `whitebox`: private helpers, architecture, or internal route tables.

Keep reusable non-fixture fakes in a named support module beside the tests that
share them. Reserve `conftest.py` for pytest hooks and injectable fixtures.

## Real-system smoke tests

Smoke scripts are opt-in and are not part of pytest discovery. They may access
private content, spend paid API quota, download models, or require Docker,
native libraries, network routes, and credentials. Every module with smoke
checks therefore owns an advanced setup runbook:

- [Connector smoke setup](connectors/smoke/README.md)
- [Model smoke setup](models/smoke/README.md)
- [Parser smoke setup](parsers/smoke/README.md)
- [Repository smoke setup](repositories/smoke/README.md)

Use least-privilege credentials and disposable resources. Never commit populated
dotenv files or capture authorization headers, source content, prompts,
responses, vectors, or raw provider payloads. Run one configured target before
a group runner and review provider pricing and model-download requirements.

Connector, model, and repository scripts read exported variables first, then
`HARBOR_SMOKE_ENV_FILE`, then the repo-root `.env`. Parser smoke checks take a
real file path and explicit options instead. The tracked templates live under
`env-example/`.

Smoke exit codes are consistent: `0` means the real operation passed, `1` means
a configured operation failed, and `2` means a prerequisite or configuration
is unavailable. Each module's smoke README documents exact installation,
configuration, safety, commands, success criteria, and troubleshooting.
