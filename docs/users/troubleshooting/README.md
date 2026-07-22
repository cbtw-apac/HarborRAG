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

## CLI and Temporal runtime

- `doctor` reports `app_test_double` in an unconfigured development checkout:
  expected; production composition requires a control database.
- The Temporal worker exits at startup: verify the connector, parser, and model
  config paths, model credentials, and Qdrant/FalkorDB endpoints. A custom
  dependency provider is optional.
- Temporal starts but no HarborRAG worker appears: set `TEMPORAL_START_WORKER=1`
  in `env/.env.temporal` after configuring the worker files and credentials.
- `harbor` is not found: use `uv run --package harborrag-app harbor` from the
  workspace or reinstall `harborrag-app` so its console script is registered.
- An ingestion command cannot connect: set `HARBORRAG_TEMPORAL_TARGET` to a
  reachable Temporal frontend (normally `localhost:7233` from the host).

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

The database and PostgreSQL-backed Temporal Compose stacks are usable for local
development. The Temporal stack is not a production topology; use Temporal
Cloud or the official Helm chart with managed persistence. See
[Deployment](../../developers/deployment/README.md) before building an
application image.
