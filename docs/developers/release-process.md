# Release Process

This page is for maintainers who publish HarborRAG packages to PyPI. Contributors do not need
it; the pull-request path is in [Contributing](../../CONTRIBUTING.md).

A release moves the workspace and its eight public packages to one version: you prepare the version bump on a branch,
merge it, then publish from `main`. `release.py` drives both halves. Release only from a
reviewed, clean commit.

## 1. Pass the full local gate

Install every package with every extra, then run each command listed under
[Quality gates](../../CONTRIBUTING.md#quality-gates):

```bash
uv sync --all-packages --all-extras
```

## 2. Prepare release metadata on a branch

Add the new version's section to [`CHANGELOG.md`](../../CHANGELOG.md) first; preparation
stops when that section is missing. Then run the preparation command. It asks for
confirmation, then changes the package versions, the internal dependency pins, the
TypeScript client version in `clients/typescript/package.json` and `package-lock.json`, the
PyPI classifiers when you pass `--development-status`, and `uv.lock`. It never commits, pushes,
tags or publishes:

```bash
uv run python release.py --dry-run --bump patch --verbose
uv run python release.py --bump patch --verbose
```

`--bump` takes `major`, `minor`, `patch` or `beta`; `--version` sets an explicit version
instead. `uv run python release.py --help` lists every option.

The bump rewrites `uv.lock`, so run the [Quality gates](../../CONTRIBUTING.md#quality-gates)
again, then open a pull request and review the changes.

### Version formats

Python distributions use [PEP 440](https://peps.python.org/pep-0440/) versions. The release
command accepts a friendly value such as `2.0.0-alpha` and normalizes it to `2.0.0a1`. Built
distributions and package tags use the PEP 440 form; the TypeScript client in
[`clients/typescript`](../../clients/typescript) uses the [SemVer](https://semver.org/)
equivalent, `2.0.0-alpha.1`.

## 3. Publish from a clean `main`

After the release commit is merged and every required workflow has passed on that exact
commit:

```bash
git switch main
git pull --ff-only
git status --short
uv run python release.py --publish --dry-run --verbose
uv run python release.py --publish --verbose
```

Publishing changes no repository files. Before it does anything, it checks that:

- the worktree is clean and on `main`, with `main` up to date and no unpushed commits;
- the required workflows passed on the current commit;
- every package has the same version;
- the changelog has a section for that version;
- no release tag for that version exists yet;
- `GITHUB_TOKEN` or `GH_TOKEN` holds a token authorized for this repository.

After you confirm, it creates and pushes one annotated tag per package, named
`<package-name>-v<version>`, and creates a GitHub release for each tag. Each release starts
`.github/workflows/publish.yml`, which reads the package name from the tag and publishes that
package. The `harborrag-v<version>` release also deploys the documentation site through
`.github/workflows/docs-auto.yml`.

## 4. First publication of a package name

Before the first PyPI publication of a package name, register a pending
[Trusted Publisher](https://docs.pypi.org/trusted-publishers/) for it on PyPI. HarborRAG
needs one per public package: `harborrag-core`, `harborrag-adapters`, `harborrag-memory`,
`harborrag-engine`, `harborrag-runtime`, `harborrag-app`, `harborrag-mcp-server` and
`harborrag`, each with these claims:

| Claim | Value |
| --- | --- |
| owner | `cbtw-apac` |
| repository | `HarborRAG` |
| workflow | `publish.yml` |
| environment | `pypi-publish`, unless the `PYPI_ENVIRONMENT` repository variable names another environment, configured the same way in GitHub and PyPI |

Pending publishers are PyPI account configuration. This repository does not create them, and
they do not reserve a package name until its first successful publication.

## Related

- [Contributing](../../CONTRIBUTING.md): the pull-request path and quality gates.
- [Open-Source Publication Guidelines](publication-guidelines.md): what may appear in public
  docs.
- [Testing](testing/README.md): the test suite the release depends on.
- [Deployed ingestion smoke](../../packages/harborrag-runtime/tests/runtime_ingestion/smoke/README.md):
  the manual end-to-end check against a running stack.
