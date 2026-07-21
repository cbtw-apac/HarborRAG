# Troubleshooting

## Installation and imports

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| `ModuleNotFoundError: harborrag_*` | Wrong interpreter or unsynchronized editable workspace | Use `uv run ...`, activate `.venv`, or run `make bootstrap` |
| `uv` cannot create a cache/lock file | Global uv cache is not writable | Point `UV_CACHE_DIR` to a writable project or temporary directory |
| An optional parser/provider import fails | The family extra or native runtime is missing | Install the matching `harborrag-adapters[...]` extra and backend prerequisites |
| Advanced PDF parsing downloads or runs slowly | Selected backend requires models/OCR | Start with the `fast` PDF profile; pre-download assets only for the chosen backend |

## Configuration

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| Connector example fails to load | Documentation path was copied without the `.example` suffix | Load or copy `config/connectors.example.yaml` |
| Local connector build reports `LOCAL_SOURCE_PATH` missing | Example maps `source_path` to that variable | Export it, or pass a build override |
| Model `validate` reports a missing environment variable | Model references are expanded eagerly | Export every `${VARIABLE}` used by the selected family/profile |
| Parser reports two enabled definitions for one type | More than one enabled profile replaces the same parser | Enable only one definition per stable parser name |
| Unknown fields are rejected | Configuration models use strict validation | Fix the spelling or move Python-only callbacks into code overrides |

Example files are not loaded automatically. HarborRAG reads the process environment; use your shell, application bootstrap, container runtime, or secret manager.

## CLI and local pipeline

- `doctor` reports `mock_runtime`: expected; the current default composition is deliberately local.
- The pipeline reports `indexed: 0`: expected; it demonstrates parsing and retrieval without repository persistence.
- `harbor` is not found: use `python -m harborrag_app.cli.main`; no console script is declared yet.
- `ingest`, `retrieve`, or `status` is rejected: these command modules are stubs and are not registered.

## Connectors and real services

- Remote connector tests need least-privilege credentials, source identifiers, and network access.
- Repository smoke checks need the appropriate SDK extra and a reachable service. Use `docker-compose.database.yml` for the local database stack.
- An unavailable smoke target exits with code 2 by design; this is distinct from a failed real operation.
- Default pytest tests should never depend on these services.

See the [smoke-test section](../../developers/testing/README.md#real-system-smoke-checks).

## Quality gates

| Failure | What to check |
| --- | --- |
| `make coverage` below 90% | Add package-local tests for new source branches |
| `make deps-check` | Compare the import against the architecture allowed-import table |
| `make typecheck` | Source functions require complete annotations under the root mypy policy |
| `make lint` | Run `ruff check .`; use `make format` only when you intend to modify formatting |
| Website build | Verify relative Markdown links and run the root website tests |

## Deployment surprises

Only the database Compose stack is documented as a usable local provider stack. API, CLI, MCP, and Temporal Dockerfiles reference entry points or extras that are not implemented in the current packages. See [Deployment](../../developers/deployment/README.md) before attempting an application image.
