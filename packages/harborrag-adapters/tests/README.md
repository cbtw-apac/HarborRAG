# harborrag-adapters tests

Find the tests for an adapter module, run them, and reach the setup guide for its opt-in checks
against real systems. Workspace-wide test commands, coverage and the marker list are in
[Testing](../../../docs/developers/testing/README.md).

## Layout

Tests are grouped by the production module that owns the behaviour, then by test type, so each
module keeps its deterministic, resilience, performance and real-system checks together.

```text
tests/
  adapters/       unit/
  chunking/       unit/
  connectors/     unit/  failure/  security/  performance/  smoke/
  models/         unit/  contract/  chaos/  performance/  smoke/
  parsers/        unit/  failure/  security/  performance/  smoke/
  repositories/   unit/  integration/  smoke/
  conftest.py                 shared pytest hooks and fixtures
  harbor_test_builders.py     Office, EPUB and zip-bomb file builders, fake HTTP session
  cfb_writer.py               OLE2 compound-file writer for legacy Office fixtures
  legacy_office_builders.py   minimal Word 97 .doc and PowerPoint .ppt files
```

| Module | What its tests cover |
| --- | --- |
| [Adapters](adapters/README.md) | Base connector, parser and repository contracts, and one connector feeding a parser |
| [Chunking](chunking/README.md) | Splitters, refiners and chunker routing |
| [Connectors](connectors/README.md) | Provider connectors, HTTP behaviour, attachments and real-source checks |
| [Models](models/README.md) | Shared model runtime, chat, embedding and reranking clients, contracts and fault injection |
| [Parsers](parsers/README.md) | Format routing, extraction, hardening, scale and real-document checks |
| [Repositories](repositories/README.md) | Storage providers, control-plane persistence and live backend checks |

## Test types

- `unit`: deterministic behaviour with fakes or local in-process dependencies.
- `integration`: several real local layers composed, such as migrations and SQLAlchemy
  repositories over a temporary SQLite database.
- `contract`: one conformance suite run against every implementation of an interface.
- `failure`, `security` and `chaos`: resilience and hardening checks.
- `performance`: bounded concurrency, scale and load checks for correctness, not capacity.
- `smoke`: standalone scripts that call real services, providers or documents.

Every collected test is deterministic: no network, credentials or model downloads. A fake
provider client belongs in `unit`, not `smoke`. Keep a fake that several test files share in a
named support module beside them, and keep `conftest.py` for pytest hooks and fixtures.

## Run the tests

Run these from the repository root.

```bash
uv run pytest packages/harborrag-adapters/tests
uv run pytest packages/harborrag-adapters/tests/connectors
uv run pytest packages/harborrag-adapters/tests/models/unit
```

The default run deselects tests marked `integration`, so the `repositories/integration/`
tests run only when you select them. Run a suite by its marker:

```bash
uv run pytest packages/harborrag-adapters/tests/repositories/integration -m integration
uv run pytest packages/harborrag-adapters/tests/models/contract -m contract
uv run pytest packages/harborrag-adapters/tests/models/chaos -m chaos
uv run pytest packages/harborrag-adapters/tests/models/performance -m load
uv run pytest packages/harborrag-adapters/tests/parsers/performance -m slow
```

What each marker means, including `blackbox`, `graybox` and `whitebox`, is in
[Markers](../../../docs/developers/testing/README.md#markers). The 90% coverage gate applies to
the combined data of all packages, not to this package alone; see
[Coverage](../../../docs/developers/testing/README.md#coverage).

## Real-system smoke tests

Smoke scripts are manual and opt-in, and pytest never collects them. They can read private
content, spend paid API quota, download models, or need Docker, native libraries, network access
and credentials. Each module with smoke checks has its own setup guide:

- [Connector smoke checks](connectors/smoke/README.md)
- [Model smoke checks](models/smoke/README.md)
- [Parser smoke checks](parsers/smoke/README.md)
- [Repository smoke checks](repositories/smoke/README.md)

Before you run one:

- Use least-privilege credentials and disposable resources.
- Run one configured target before a group runner, and check the provider's pricing and any
  model downloads first.
- Never commit a populated dotenv file, and never paste authorization headers, source content,
  prompts, responses, vectors or raw provider payloads into an issue or test evidence.

Which dotenv file each script family reads is in
[Real-system smoke checks](../../../docs/developers/testing/README.md#real-system-smoke-checks).

Every smoke script ends with the same exit codes:

| Code | Meaning |
| --- | --- |
| `0` | The real operation passed |
| `1` | The target was configured, but the operation or its validation failed |
| `2` | A prerequisite or setting is missing, so nothing was checked; this is not a pass |
