# Contributing to HarborRAG

Set up a development checkout, find where a change belongs, run the checks CI runs, and open
a pull request. HarborRAG is a multi-package Python workspace, so a good change stays inside
the package that owns the behaviour, adds tests in that package, and updates the
documentation and configuration examples it affects.

## Development setup

Get a checkout and its local stack running with the
[Quick Start](docs/getting-started/quick-start.md), then add the lint, type-check and test
tools as in
[Running HarborRAG from a checkout](docs/developers/checkout-quick-start.md#1-install-the-development-tools):

```bash
uv sync --all-packages --extra dev --extra production
```

Install the git hooks:

```bash
uv run pre-commit install -t pre-commit -t pre-push
```

`make hooks` runs the same command where `make` is available. [Git hooks](#git-hooks) lists
what each stage runs.

Without uv, install the packages with pip and `make bootstrap` as in
[Installation, Option B](docs/getting-started/installation.md#option-b-editable-pip-install).

Publishing a release is a separate, maintainer-only flow:
[Release process](docs/developers/release-process.md).

## Before changing code

1. Read the relevant package README and [Architecture](docs/developers/architecture/README.md)
   for package ownership and import direction.
2. Check `git status` and keep unrelated local changes intact.
3. Find the closest existing implementation and its tests before adding a new pattern.

Keep changes focused. Do not combine generated files, broad formatting or unrelated
refactors with a feature or fix.

## Package ownership

[Architecture](docs/developers/architecture/README.md#package-map) lists what each of the
eight packages owns and
[which imports between them are allowed](docs/developers/architecture/README.md#dependency-direction).
After changing an import that crosses packages, run:

```bash
uv run make import-boundaries
uv run make deps-check
```

## Developer map

| Goal | Location |
| --- | --- |
| Add a connector | `packages/harborrag-adapters/src/harborrag_adapters/connectors/<provider>/` |
| Add a PDF parser backend | `packages/harborrag-adapters/src/harborrag_adapters/parsers/pdf/engines/<backend>/` |
| Change Confluence canonical normalization | `packages/harborrag-adapters/src/harborrag_adapters/connectors/confluence/normalization/` |
| Add a vector repository | `packages/harborrag-adapters/src/harborrag_adapters/repositories/vector/<backend>/` |
| Add a chat or model provider | `packages/harborrag-adapters/src/harborrag_adapters/models/<family>/` |
| Add a source chunking strategy | `packages/harborrag-engine/src/harborrag_engine/ingestion/chunking/sources/` |
| Change ingestion business behaviour | `packages/harborrag-engine/src/harborrag_engine/ingestion/` |
| Change retrieval ranking | `packages/harborrag-engine/src/harborrag_engine/retrieval/` |
| Add an API endpoint | `packages/harborrag-app/src/harborrag_app/api/v1/` |
| Add a CLI command | `packages/harborrag-app/src/harborrag_app/cli/commands/` |
| Add an MCP tool | `packages/harborrag-engine/src/harborrag_engine/tools/` |
| Add durable execution | `packages/harborrag-runtime/src/harborrag_runtime/temporal/` |

A new backend adds its implementation folder, a configuration example and its contract-test
registration. It should not require changes to engine pipelines, transports or unrelated
providers. The one current exception is provider metadata for the LiteLLM chat backends,
which stays in `models/chat/registry.py`; a second execution provider path needs a feature
design before a plugin registry is introduced.

## Adapter contributions

[Extending HarborRAG](docs/developers/extending/README.md) describes what a connector,
parser, model provider or repository must implement and test. In short:

- Keep provider SDK imports inside `harborrag-adapters`, and return HarborRAG domain or
  schema objects rather than raw provider responses.
- Declare an optional dependency when a new SDK is required.
- Test with fake SDK clients. Never require live credentials in the default test run, and
  never commit real provider payloads that hold sensitive data.
- Add a safe example configuration or environment key when operators need one.

## Testing

Add tests in the package that owns the behaviour:

```text
packages/<package>/tests/   package behaviour
tests/                      cross-package and website behaviour
```

Useful commands:

```bash
uv run make test
uv run make test-package PACKAGE=harborrag-core
uv run make coverage
uv run pytest packages/harborrag-adapters/tests/connectors/unit/
uv run pytest -m "not integration and not e2e and not smoke and not slow"
```

A `-m` expression on the command line replaces the default selection, which leaves out
`integration`, `e2e` and `smoke` tests, so the last command repeats those exclusions before
it adds `slow`.

The default suite must be deterministic and work without Docker, network access, cloud
accounts or paid APIs. A test that needs one of those is opt-in: mark it `integration`, or
write it as a standalone script in the owning `packages/<package>/tests/<module>/smoke/`
folder. [Testing](docs/developers/testing/README.md) lists the markers and the real-system
smoke commands.

## Quality gates

Run the checks relevant to your change. Before a pull request, sync every extra, as CI does,
and run the full set:

```bash
uv sync --all-packages --all-extras
uv run make lint
uv run make complexity
uv run make file-length
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

Review the diff after formatting. `make lint` requires zero non-complexity Ruff findings.
`make complexity` enforces the committed per-file `C901`/`PLR0913` ratchet: violations may
only decrease, and a reduction must update `.ruff-complexity-baseline.json` with
`uv run python scripts/check_ruff_complexity.py --write-baseline`. `make file-length` keeps
every Python file at 1000 lines or fewer. `uv run make help` lists every target.

The CI quality workflow runs Ruff lint, the complexity ratchet, the file-length check,
import-linter, type checking, dependency direction and compilation. The test workflow runs
each package suite once, then combines their coverage for the same 90% gate that
`make coverage` applies locally; it also runs the website and release-tooling tests with
their own coverage floors. Configure branch protection to require the
`Quality Gates / build-test` and `Test and Coverage / Test Summary` statuses.

## Git hooks

`.pre-commit-config.yaml` runs the same gates locally, split by cost:

| Stage | Gates |
| --- | --- |
| `pre-commit` | Ruff format, Ruff check, file length, complexity ratchet, and file hygiene (trailing whitespace, end-of-file newline, YAML/TOML/JSON validity, merge conflicts, large files, private keys) |
| `pre-push` | import-linter contracts, dependency direction, compilation, then mypy |

Ruff rewrites files and then fails the commit, so you review the diff and commit again.
Nothing is staged on your behalf.

Hooks call `uv run --all-packages --all-extras` rather than `make`, so tool versions resolve
from `uv.lock` exactly as in CI, and contributors without a Bash shell get the same
behaviour.

The push stage takes roughly ten seconds. The 90% coverage gate is not part of it: fifteen
tests currently fail on Windows, five because creating a symlink needs Developer Mode and the
rest because `harborrag-mcp-server` calls `os.fchmod`, which does not exist there. A local
pytest gate would block every push from a Windows machine. Run `uv run make coverage` before
opening a pull request, and restore the hook once those failures are fixed.

To skip one gate by its id, for example while bisecting a type error:

```bash
SKIP=typecheck git push
```

`git push --no-verify` skips every push gate; keep it for emergencies. CI enforces all of
them regardless.

The hygiene hooks exclude `LICENSE`, whose text must stay byte-exact, and `website/assets/`,
a vendored icon and script library. The private-key check excludes
`packages/harborrag-core/tests/test_core_contracts_domain_security.py`, which asserts that
redaction masks a private-key header and holds no key material.

## Documentation changes

Update the documentation whenever behaviour, public imports, commands, configuration,
environment variables, dependencies or deployment status change:

- Project overview: `README.md`; first run: `docs/getting-started/quick-start.md`
- User and developer guides: `docs/`
- Package-specific API details: `packages/<package>/README.md` or the nearest module README
- Release-visible behaviour: `CHANGELOG.md`
- Architecture decisions and boundaries: `docs/developers/architecture/`
- Publication sequence (maintainers): `docs/developers/release-process.md`

Use relative Markdown links and commands that run from the repository root. Be explicit about
alpha or scaffolded surfaces; do not document a placeholder as working.

All documentation is publication material. Follow the
[open-source publication guidelines](docs/developers/publication-guidelines.md): use
synthetic data, distinguish implemented behaviour from direction, and leave out credentials,
customer or employee content, internal hosts and topology, incident material, confidential
roadmaps and unsupported claims. Do not add a private research artifact and redact it in
place; rewrite only the stable, community-useful information in the right public guide.

Build and test the documentation site with:

```bash
uv sync --all-packages --extra dev --extra docs
uv run python website/build.py --output site --templates website/templates
uv run pytest tests/test_website_*.py tests/test_link_checker*.py
uv run python website/check_branding.py
uv run python website/check_publication.py
```

The `docs` extra matters: without `markdown`, `pygments` and `pymdown-extensions` the builder
falls back to a degraded renderer, in which tables render as raw `|` pipes and heading anchors
are lost, while still reporting success. It prints a warning, so read the build output rather
than only its exit code.

Collapsible sections need `<details markdown="1">`, not a bare `<details>`, or the site
builder does not process the Markdown inside them.

## Configuration and secrets

- Keep secrets out of YAML, examples, logs, fixtures and exception messages.
- Use `${ENVIRONMENT_VARIABLE}` references for model configuration and the documented `*_env`
  mapping for connector and parser secrets.
- Make example credentials unmistakable placeholders.
- Do not rely on ambient cloud credentials unless the configuration explicitly opts in.
- Pass potentially sensitive diagnostic text through the established redaction helpers.

## TODO comments

A TODO names its scope and a concrete next action:

```python
# TODO(connectors/jira): Add incremental sync using the updated timestamp cursor.
# TODO(parsers/pdf): Preserve table bounding boxes exposed by layout-aware backends.
```

Avoid vague notes such as `TODO: improve this`. `make provider-matrix` lists the scoped TODOs
across the packages.

## Commits and pull requests

Prefer small, imperative commits with a type and an optional scope:

```text
feat(runtime): rerank retrieved passages with the catalog's rerank model
fix(cli): reject a source type passed as a connection key
docs(settings): list every runtime setting and its default
```

A pull request explains the behaviour change, the package boundaries it affects, the tests
you ran, any configuration or migration impact, and any intentionally unfinished follow-up.
Complete `.github/PULL_REQUEST_TEMPLATE.md`, including its Docs Impact section.

## Pull request checklist

- [ ] The change is in the package that owns the behaviour.
- [ ] The public contract and non-goals are documented.
- [ ] Cross-package imports pass `make import-boundaries` and `make deps-check`.
- [ ] No logic is duplicated from another package.
- [ ] Data shapes live in capability-local `schemas.py` files.
- [ ] `__all__` appears only in `__init__.py`.
- [ ] Configuration and failure-path examples are covered.
- [ ] Unit and applicable contract or integration tests are present.
- [ ] Observability is included where relevant.
- [ ] Replaced code is removed without compatibility aliases.
- [ ] No unused files or duplicate implementations remain.
- [ ] Relevant README and ADR records are updated.
- [ ] Public text and fixtures pass the publication checklist and contain only synthetic data.
- [ ] Security-sensitive reports follow [SECURITY.md](SECURITY.md) instead of a public issue.
- [ ] Lint, complexity, type checking, compilation, tests and coverage pass.
