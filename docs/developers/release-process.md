# Release Process

This page is for maintainers publishing HarborRAG packages to PyPI. Contributors do not
need it - see [Contributing](../../CONTRIBUTING.md) for the pull-request path.

Release only from a reviewed, clean commit.

## 1. Pass the full local gate

The local gate mirrors the repository CI:

```bash
uv sync --all-packages --all-extras
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

## 2. Prepare release metadata on a branch

Add the new version section to [`CHANGELOG.md`](../../CHANGELOG.md) first, then run the
preparation command. It changes package versions, internal dependency pins, the TypeScript
client version, classifiers when requested, and `uv.lock`. It never commits, pushes, tags,
or publishes:

```bash
uv run python release.py --dry-run --bump patch --verbose
uv run python release.py --bump patch --verbose
```

Re-run the full gate afterwards, because the version bump touches `uv.lock`:

```bash
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

Open a pull request and review those changes.

### Version formats

Python distributions use PEP 440 prerelease versions. The release command accepts a
friendly value such as `2.0.0-alpha` and normalizes it to canonical `2.0.0a1`. Built Python
distributions and package tags use the canonical form; the synchronized TypeScript client
in [`clients/typescript`](../../clients/typescript) uses the SemVer equivalent
`2.0.0-alpha.1`.

## 3. Publish from a clean `main`

After the release commit is merged and every required workflow passes on that exact commit:

```bash
git switch main
git pull --ff-only
git status --short
uv run python release.py --publish --dry-run --verbose
uv run python release.py --publish --verbose
```

Publishing does not modify repository files. It requires:

- synchronized package versions across the workspace
- an updated changelog
- absent release tags for the target version
- a clean `main` with no unpushed commits
- passing workflows on the current commit
- `GITHUB_TOKEN` or `GH_TOKEN` authorized for this repository

Tags follow the `release.py` convention `<package-name>-v<version>`, which is what
`.github/workflows/publish.yml` matches to decide which package to build.

## 4. First publication of a package name

For the first PyPI publication of a package name, register a pending Trusted Publisher
before creating its GitHub release. HarborRAG needs one publisher per public package -
`harborrag-core`, `harborrag-adapters`, `harborrag-memory`, `harborrag-engine`,
`harborrag-runtime`, `harborrag-app`, `harborrag-mcp-server`, and `harborrag` - with these
claims:

| Claim | Value |
| --- | --- |
| owner | `cbtw-apac` |
| repository | `HarborRAG` |
| workflow | `publish.yml` |
| environment | `pypi-publish`, unless `PYPI_ENVIRONMENT` is configured to the same alternate environment in both GitHub and PyPI |

Pending publishers are external PyPI account configuration. They are not created by this
repository and do not reserve package names until the first successful publication.

## Related

- [Contributing](../../CONTRIBUTING.md) - pull-request and quality gates
- [Open-source publication guidelines](publication-guidelines.md) - what may appear in public docs
- [Testing](testing/README.md) - the gates the release depends on
- [Deployed ingestion smoke guide](../../packages/harborrag-runtime/tests/runtime_ingestion/smoke/README.md) - the manual staging release gate
