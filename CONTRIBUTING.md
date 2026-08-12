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

Then install the git hooks:

```bash
uv run pre-commit install -t pre-commit -t pre-push
```

`make hooks` runs the same command where `make` is available. See
[Git hooks](#git-hooks) for what each stage runs.

For a `pip` editable installation:

```bash
make bootstrap
```

`make bootstrap` installs the seven active workspace packages in dependency
order and then installs the root `dev` extra.

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
| `harborrag-engine` | Pure ingestion transformations, representation/projection mapping, retrieval, and evidence orchestration |
| `harborrag-runtime` | Configuration loading, composition, jobs, supervision, schedules, and durable-workflow boundaries |
| `harborrag-app` | Application services, CLI commands, and HTTP controllers |
| `harborrag-mcp-server` | MCP tool/server interfaces, policy, and audit behavior |
| `harborrag` | Stable public re-exports only |

The allowed HarborRAG imports are:

```text
harborrag_core      -> none
harborrag_adapters  -> core
harborrag_engine    -> core
harborrag_runtime   -> core, adapters, engine
harborrag_app       -> core, engine, runtime
harborrag_mcp_server -> core, engine, runtime
harborrag           -> any active HarborRAG package
```

Run `make deps-check` after changing cross-package imports.

## Developer map

| Goal | Location |
| --- | --- |
| Add a connector | `packages/harborrag-adapters/src/harborrag_adapters/connectors/<provider>/` |
| Add a PDF parser backend | `packages/harborrag-adapters/src/harborrag_adapters/parsers/pdf/engines/<backend>.py` |
| Change Confluence canonical normalization | `packages/harborrag-adapters/src/harborrag_adapters/connectors/confluence/normalization/` |
| Add a vector repository | `packages/harborrag-adapters/src/harborrag_adapters/repositories/vector/<backend>/` |
| Add a chat/model provider | `packages/harborrag-adapters/src/harborrag_adapters/models/<family>/` |
| Add a source chunking strategy | `packages/harborrag-engine/src/harborrag_engine/ingestion/chunking/sources/` |
| Change ingestion business behavior | `packages/harborrag-engine/src/harborrag_engine/ingestion/` |
| Change retrieval ranking | `packages/harborrag-engine/src/harborrag_engine/retrieval/` |
| Add an API endpoint | `packages/harborrag-app/src/harborrag_app/api/routes/` |
| Add a CLI command | `packages/harborrag-app/src/harborrag_app/cli/commands/` |
| Add an MCP tool | `packages/harborrag-mcp-server/src/harborrag_mcp_server/tools/` |
| Add durable execution | `packages/harborrag-runtime/src/harborrag_runtime/temporal/` |

A new backend should add its implementation folder, configuration example, and
contract-test registration. It should not require changes to engine pipelines,
transports, or unrelated providers. The one current exception is provider metadata
behind the single LiteLLM chat path, which remains in
`models/chat/registry.py`; a second execution provider path requires a feature design
before introducing a plugin registry.

## Adapter contributions

Place provider code under the matching family in `packages/harborrag-adapters/src/harborrag_adapters/`:

```text
connectors/<provider>/
parsers/ or parsers/pdf/engines/
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
uv run make complexity
uv run make import-boundaries
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

Formatting is explicit and may modify files:

```bash
uv run make format
```

Review the resulting diff after formatting. `make lint` requires zero
non-complexity Ruff findings. `make complexity` enforces the committed per-file
`C901`/`PLR0913` ratchet: violations may only decrease, and reductions must update
`.ruff-complexity-baseline.json` with
`uv run python scripts/check_ruff_complexity.py --write-baseline`.

The CI quality workflow runs Ruff lint, the complexity ratchet, import-linter, type
checking, dependency direction, compilation, and the 90% coverage gate. A second
workflow runs every active package suite plus the website tests. Configure branch
protection to require the `Quality Gates / build-test` status.

## Git hooks

`.pre-commit-config.yaml` runs the same gates locally, split by cost:

| Stage | Gates |
| --- | --- |
| `pre-commit` | Ruff format, Ruff check, file length, complexity ratchet, and file hygiene (trailing whitespace, end-of-file newline, YAML/TOML/JSON validity, merge conflicts, large files, private keys) |
| `pre-push` | import-linter contracts, dependency direction, compilation, then mypy |

Ruff rewrites files and then fails the commit, so you review the diff and
commit again. Nothing is staged on your behalf.

Hooks invoke `uv run --all-packages --all-extras` rather than `make`, so tool
versions resolve from `uv.lock` exactly as CI resolves them, and contributors
without a bash shell get identical behavior.

The push stage takes roughly ten seconds. The 90% coverage gate is not part
of it: fifteen tests currently fail on Windows, five because creating a
symlink needs Developer Mode and the rest because
`harborrag-mcp-server` calls `os.fchmod`, which does not exist there. A local
pytest gate would block every push from a Windows machine. Run
`uv run make coverage` before opening a pull request, and restore the hook
once those failures are fixed.

To skip one gate by id, for example while bisecting a type error:

```bash
SKIP=typecheck git push
```

`git push --no-verify` skips every push gate and should stay reserved for
emergencies. CI enforces all of them regardless.

The hygiene hooks exclude `LICENSE`, whose text must stay byte-exact, and
`website/assets/`, which is a vendored icon and script library. The
private-key check excludes
`packages/harborrag-core/tests/test_core_contracts_domain_security.py`, which
asserts that redaction masks a private-key header and holds no key material.

## Documentation changes

Update documentation whenever behavior, public imports, commands, configuration, environment variables, dependencies, or deployment status changes.

- Root overview and first run: `README.md`
- User and developer guides: `docs/`
- Package-specific API details: `packages/<package>/README.md` or the nearest module README
- Release-visible behavior: `CHANGELOG.md`
- Architecture decisions: `docs/adr/`

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
- [ ] The public contract and non-goals are documented.
- [ ] Cross-package imports pass `make import-boundaries` and `make deps-check`.
- [ ] No logic is duplicated from another package.
- [ ] Data shapes live in capability-local `schemas.py` files.
- [ ] `__all__` appears only in `__init__.py`.
- [ ] Configuration and failure-path examples are covered.
- [ ] Unit and applicable contract/integration tests are present.
- [ ] Observability is included where relevant.
- [ ] Replaced code is removed without compatibility aliases.
- [ ] No unused files or duplicate implementations remain.
- [ ] Relevant README and ADR records are updated.
- [ ] Lint, complexity, type checking, compilation, tests, and coverage pass.
