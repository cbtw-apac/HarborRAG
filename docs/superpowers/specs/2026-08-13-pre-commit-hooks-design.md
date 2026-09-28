# Pre-commit and pre-push quality gate hooks

Status: implemented on `chore/pre-commit_hook` (PR #28).
Date: 2026-08-13.

## Problem

Nine quality gates run in `quality-gates.yml`, and `CONTRIBUTING.md` asks
contributors to run all of them by hand before opening a pull request. Nothing
enforced that locally, so the first signal a gate had failed was a red CI run.

`pre-commit>=3.5.0` was already declared in the `dev` extra, but no
`.pre-commit-config.yaml` and no installed hook existed. The dependency had been
added and never wired up.

## Constraints discovered before designing

1. **`make` is not available on every contributor machine.** The Makefile is the
   shared contract between local development and CI, but it declares
   `SHELL := /bin/bash` and several recipes use bash-only syntax (`[[ -z ... ]]`,
   `find -exec`). Hooks therefore cannot shell out to `make`.
2. **Most gates are repository-wide, not file-scoped.**
   `check_python_file_length.py` discards `argv` and rescans the index;
   `lint-imports`, `check_dependency_direction.py`, and the complexity ratchet all
   need the whole import graph. Only Ruff is naturally per-file.
3. **`ruff format` and `ruff check` covered different paths.** `make lint`
   format-checked `packages tests scripts` while linting `.`, so `website/` and
   `release.py` were linted but never format-checked.

## Decisions

### Two stages, split by cost

| Stage | Gates | Cost |
| --- | --- | --- |
| `pre-commit` | ruff format, ruff check `--fix`, file length, complexity ratchet, hygiene (trailing whitespace, EOF newline, YAML/TOML/JSON validity, merge conflicts, large files, private keys) | ~2s |
| `pre-push` | import-linter, dependency direction, `compileall`, mypy — `fail_fast`, cheapest first | ~8s warm |
| CI only | the 90% coverage gate | ~5m19s |

Ruff rewrites files and then **fails** the commit rather than auto-staging, so no
commit contains edits nobody reviewed. This matches the existing CONTRIBUTING
instruction to review the diff after formatting.

### Hooks call `uv run`, not `make`

Every entry is `uv run --all-packages --all-extras <tool>` with
`language: system`. This resolves tool versions from `uv.lock` — the same
resolution `quality-gates.yml` uses — while working on machines without `make`.
A single uv prefix is used for every hook, including the fast ones: mixing
prefixes makes uv re-sync the same `.venv` on alternating invocations.

### `pass_filenames: false` on repository-wide gates

Passing staged paths to `lint-imports`, the ratchet, the file-length script,
`compileall`, or mypy would silently narrow their input. The affected hook ids
are listed in `REPO_WIDE_HOOK_IDS` and asserted by the drift test.

### Ruff scope made symmetric

`make lint` and `make format` now both operate on `.`, matching what
`ruff check` already did. The previous hand-maintained path list was the defect;
`[tool.ruff] extend-exclude` already covers virtualenvs. Reformatting
`website/build.py` and `website/check_links.py` landed as a separate commit
before the hooks, so the first hook run was clean.

### Config bound to CI by a test

`tests/test_pre_commit_config.py` asserts that every `uv run make <target>` step
in `quality-gates.yml` maps to a hook id or appears in `CI_ONLY_TARGETS` with a
stated reason; that repository-wide hooks keep `pass_filenames: false`; that
expensive hooks carry `stages: [pre-push]`; that the Ruff hooks use the same
`--ignore C901,PLR0913` as the Makefile; that every entry resolves through
`uv run`; and that `minimum_pre_commit_version` is satisfiable by the
`pre-commit` floor declared in `pyproject.toml`.

A hook that silently disagrees with CI is worse than no hook, because it buys
false confidence. Two real defects of exactly that shape occurred during
implementation — `pass_filenames: true` on `import-boundaries`, and a
`minimum_pre_commit_version` of 4.0.0 against a declared floor of 3.5.0 — and
both are now covered.

## Deviation from the approved design

The design approved a **full test suite plus the 90% coverage gate on
pre-push**. What shipped keeps coverage in CI only.

The reason is platform, not runtime: eight tests fail on Windows, so a local
pytest gate would block every push from a Windows machine. The suite itself runs
in about 49 seconds with `-n 4`, so the multi-minute cost that motivated the
original concern does not exist.

This is recorded in `CI_ONLY_TARGETS` with its reason so the exemption stays
deliberate and the drift test keeps it visible. Restore the hook once the eight
failures below are resolved.

## Pre-existing bugs surfaced

These hooks were the first thing to execute this tooling on Windows. Three
defects had been latent because CI only ever ran Linux.

1. **The complexity ratchet had never run on Windows.** It passed every
   repository Python file as argv — 116,312 characters against the 32,767
   `CreateProcess` limit. Now scans `.` and lets Ruff discover files; output is
   identical (55 diagnostics across 42 files, matching the committed baseline).
2. **The durable MCP audit log and configuration store crashed on Windows.**
   `_open_audit_file` opened the parent directory as a descriptor, which Windows
   refuses with `EACCES`, and both modules called `os.fchmod`, which does not
   exist there. A `dir_fd`-gated fallback now mirrors the pattern
   `connectors/local/secure_read.py` already uses: it narrows rather than closes
   the TOCTOU window. `_write_configuration` also leaked its `mkstemp`
   descriptor on any failure before `os.fdopen`, which on Windows made the
   cleanup `unlink` fail with `WinError 32` and mask the original error.
3. **mypy reported phantom errors on Windows.** These were briefly suppressed
   with `platform = "linux"`, which also suppressed the genuine `os.fchmod`
   defects in item 2. Replaced with a narrower fix: MinerU's guard now uses
   `sys.platform`, which mypy narrows, instead of `os.name`, which it does not.
   mypy is clean on both the host platform and `--platform linux`.

## Known gaps

- Eight tests fail on Windows: five need symlink-creation privileges (Developer
  Mode), one asserts prometheus process metrics that exist only on POSIX, and two
  are unrelated platform gaps in `harborrag-app` and the compose asset test.
- The pre-push stage does not run tests at all. Coverage remains CI-enforced.

## Files

| File | Role |
| --- | --- |
| `.pre-commit-config.yaml` | Both stages |
| `tests/test_pre_commit_config.py` | Binds the config to CI |
| `Makefile` | Symmetric Ruff scope; `hooks` install target |
| `.github/workflows/quality-gates.yml` | `pre-commit validate-config` step |
| `CONTRIBUTING.md` | Install instructions and the `SKIP=<id>` bypass |

## Bypasses

`SKIP=<hook-id> git push` skips one gate and keeps the rest. `--no-verify` skips
everything and should be reserved for emergencies. CI remains the enforcing
authority in both cases.
