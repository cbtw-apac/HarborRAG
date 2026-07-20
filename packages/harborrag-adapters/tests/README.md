# Adapter test strategy

`harborrag-adapters` has one test root. Tests are grouped first by execution
type and then by the production module they exercise.

```text
tests/
  unit/
    adapters/       package builder, registries, repository contracts/test doubles
    connectors/     provider connectors and shared connector utilities
    models/         shared model runtime plus chat, embed, and rerank clients
    parsers/        format parsers, routing, and PDF engines
  contracts/
    models/chat/     reusable behavioral contract for each chat backend
  failure/           error normalization and deterministic recovery behavior
  security/          hostile input, path, URL, XML, archive, and secret handling
  performance/       bounded local scale, concurrency, and micro-load checks
  chaos/             deterministic fault injection and cancellation recovery
  smoke/             standalone checks against real systems and real files
```

There is intentionally no `tests_model/` tree. Model tests follow the same
type-first layout as every other adapter module.

## What belongs where

| Production area | Deterministic test location | Live smoke location |
| --- | --- | --- |
| `builder.py` and top-level registries | `unit/adapters/` | Exercised through provider-family smoke checks |
| `connectors/` | `unit/connectors/`, `failure/`, `security/` | `smoke/connectors/` |
| `parsers/` | `unit/parsers/`, `failure/`, `security/`, `performance/` | `smoke/parsers/` |
| `models/chat/` | `unit/models/chat/`, `contracts/models/chat/` | `smoke/models/chat.py` |
| `models/embed/` | `unit/models/embed/` | `smoke/models/embed.py` |
| `models/rerank/` | `unit/models/rerank/` | `smoke/models/rerank.py` |
| `models/common/` and CLI/runtime composition | `unit/models/`, `chaos/models/`, `performance/models/` | Exercised through each live model family |
| `repositories/` | `unit/repositories/` | `smoke/repositories/` |

Unit, failure, security, contract, chaos, and performance tests use pytest and
must be deterministic. Provider tests that use fake clients remain unit tests;
they are not smoke tests.

Smoke checks do not use pytest or mocks. They execute public adapter APIs
against real services or real local documents and are never part of normal
test discovery. See [the smoke-test runbook](smoke/README.md).

## Commands

Run the complete deterministic adapter suite from the repository root:

```bash
python -m pytest packages/harborrag-adapters/tests
```

Run one type or module:

```bash
python -m pytest packages/harborrag-adapters/tests/unit
python -m pytest packages/harborrag-adapters/tests/unit/connectors
python -m pytest packages/harborrag-adapters/tests/unit/models
python -m pytest packages/harborrag-adapters/tests/unit/parsers
python -m pytest packages/harborrag-adapters/tests/contracts -m contract
python -m pytest packages/harborrag-adapters/tests/chaos -m chaos
python -m pytest packages/harborrag-adapters/tests/performance -m performance
```

Behavior markers describe the observation boundary when useful:

- `blackbox`: public API behavior only.
- `graybox`: public behavior plus observable logs, routes, or injected clients.
- `whitebox`: private helpers, architecture, or internal route tables.

Keep reusable non-fixture fakes in a named support module beside the tests that
share them. Reserve `conftest.py` for pytest hooks and injectable fixtures.
