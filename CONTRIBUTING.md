# Contributing to HarborRAG

Thank you for contributing. HarborRAG is a multi-package Python workspace, so a good change preserves package boundaries, includes tests at the owning layer, and updates the relevant documentation and configuration examples.

## Development setup

Requirements:

- Python 3.12 or newer
- `uv` (recommended) or `pip`

Set up the workspace with `uv`:

```bash
uv sync --all-packages --extra dev
uv run python -m harborrag_app.cli.main doctor --json
uv run pytest
```

For a `pip` editable installation:

```bash
make bootstrap
```

`make bootstrap` installs the seven active workspace packages in dependency order and then installs the root `dev` extra. The `harborrag-memory` directory is currently a placeholder and is not a uv workspace member.

## Before changing code

1. Read the root [README](README.md) and the relevant package README.
2. Check [Architecture](docs/developers/architecture/README.md) for ownership and import direction.
3. Check `git status` and preserve unrelated local changes.
4. Find the closest existing implementation and tests before adding a new pattern.

Keep changes focused. Do not combine generated files, broad formatting, or unrelated refactors with a feature or fix.

## Package ownership

| Package | Owns |
| --- | --- |
| `harborrag-core` | Provider-neutral domain objects, shared schemas, model contracts, errors, and security helpers |
| `harborrag-adapters` | External connectors, parsers, model transports, repository providers, and provider validation |
| `harborrag-engine` | Ingestion, retrieval, indexing, graph mapping, and evidence orchestration |
| `harborrag-runtime` | Configuration loading, composition, jobs, supervision, schedules, and durable-workflow boundaries |
| `harborrag-app` | Application services, CLI commands, and HTTP controllers |
| `harborrag-mcp` | MCP tool/server interfaces, policy, and audit behavior |
| `harborrag` | Stable public re-exports only |

The allowed HarborRAG imports are:

```text
harborrag_core      -> none
harborrag_adapters  -> core
harborrag_engine    -> core, adapters
harborrag_runtime   -> core, adapters, engine
harborrag_app       -> core, engine, runtime
harborrag_mcp       -> core, engine, runtime
harborrag           -> any active HarborRAG package
```

Run `make deps-check` after changing cross-package imports.

## Adapter contributions

Place provider code under the matching family in `packages/harborrag-adapters/src/harborrag_adapters/`:

```text
connectors/<provider>/
parsers/ or parsers/pdf_engine/
models/{chat,embed,rerank}/
repositories/{vector,graph,cache,object_store,database,state}/<provider>/
```

An adapter change should normally include:

- typed configuration with early validation;
- public exports or registry/plugin registration;
- deterministic unit, failure, and security tests using fake SDK clients;
- an optional-dependency declaration when a new SDK is required;
- a safe example configuration or environment key when operators need one;
- normalized HarborRAG domain or schema outputs rather than raw provider responses.

Keep provider SDK imports inside adapters. Never require live credentials in default pytest collection, and never commit real provider payloads containing sensitive data.

Repository operations must accept `StorageOperationContext` and preserve tenant isolation. Use `repositories/`, not a new `stores/` top-level family.

## Testing

Add tests in the package that owns the behavior:

```text
packages/<package>/tests/   package behavior
tests/                      cross-package and website behavior
```

Useful commands:

```bash
uv run make test
uv run make test-package PACKAGE=harborrag-core
uv run make coverage
uv run pytest packages/harborrag-adapters/tests/connectors/unit/
uv run pytest -m "not integration and not slow"
```

The default suite must be deterministic and work without Docker, network
access, cloud accounts, or paid APIs. Tests needing those boundaries must be
opt-in and marked `integration`, or implemented as a standalone script under
the owning `packages/harborrag-adapters/tests/<module>/smoke/` directory.

See [Testing](docs/developers/testing/README.md) for markers and real-system smoke commands.

## Quality gates

Run the checks relevant to your change. Before a pull request, the full local set is:

```bash
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

Formatting is explicit and may modify files:

```bash
uv run make format
```

Review the resulting diff after formatting. The CI quality workflow runs lint, type checking, dependency direction, compilation, and the 90% coverage gate. A second workflow runs every active package suite plus the website tests.

## Documentation changes

Update documentation whenever behavior, public imports, commands, configuration, environment variables, dependencies, or deployment status changes.

- Root overview and first run: `README.md`
- User and developer guides: `docs/`
- Package-specific API details: `packages/<package>/README.md` or the nearest module README
- Release-visible behavior: `CHANGELOG.md`

Use repository-relative Markdown links and runnable commands from the repository root. Be explicit about alpha or scaffolded surfaces; do not document a placeholder as operational.

Build and test the documentation site with:

```bash
uv run python website/build.py --output site --templates website/templates
uv run pytest tests/
```

## Configuration and secrets

- Keep secrets out of YAML, examples, logs, fixtures, and exception messages.
- Use `${ENVIRONMENT_VARIABLE}` references for model configuration and the documented `*_env` mapping for connector/parser secrets.
- Example credentials must be unmistakable placeholders.
- Do not rely on ambient cloud credentials unless the configuration explicitly opts in.
- Pass potentially sensitive diagnostic text through the established redaction helpers.

## TODO comments

A TODO should name its scope and define a concrete next action:

```python
# TODO(connectors/jira): Add incremental sync using the updated timestamp cursor.
# TODO(parsers/pdf): Preserve table bounding boxes exposed by layout-aware backends.
```

Avoid vague notes such as `TODO: improve this`. `make provider-matrix` lists scoped TODOs across the packages.

## Commits and pull requests

Prefer small, imperative commits. The repository commonly uses a scoped prefix, for example:

```text
adapters: validate GitHub pagination cursors
runtime: reject duplicate parser definitions
docs: refresh local repository smoke instructions
```

A pull request should explain the behavior change, package boundaries affected, tests run, configuration or migration impact, and any intentionally unfinished follow-up. Complete `.github/PULL_REQUEST_TEMPLATE.md`, including its documentation-impact section.

## Pull request checklist

- [ ] The change is in the package that owns the behavior.
- [ ] Cross-package imports pass `make deps-check`.
- [ ] New behavior has deterministic tests at the owning layer.
- [ ] Live services and credentials are not required by default tests.
- [ ] Public behavior, configuration, and examples are documented.
- [ ] Secrets and provider payloads are absent from the diff.
- [ ] Lint, type checking, compilation, and relevant tests pass.
- [ ] Coverage remains at or above 90%.
