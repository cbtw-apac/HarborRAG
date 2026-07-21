# CLI Reference

The application CLI is implemented in `harborrag_app.cli.main`. No console-script entry point is declared in the package metadata yet, so invoke it as a module.

## `doctor`

```bash
uv run python -m harborrag_app.cli.main doctor [--json]
```

Calls the application service's health method and returns local engine/runtime diagnostics. `--json` emits JSON; without it, the command prints a Python dictionary.

## `sample-ingest`

```bash
uv run python -m harborrag_app.cli.main sample-ingest [--json]
```

Calls the mock application service's one-shot ingestion method. This is a deterministic application-boundary demonstration, not a configurable source-to-repository ingestion command.

## Model configuration CLI

The adapter package has a separate configuration utility:

```bash
uv run python -m harborrag_adapters.models validate FILE --family chat
uv run python -m harborrag_adapters.models explain FILE --family embed
uv run python -m harborrag_adapters.models render FILE --family rerank --format json
```

Families are `chat`, `embed`, and `rerank`. Add `--profile NAME` when the document defines profiles. `render` accepts `--output PATH`; without it, sanitized configuration is printed. Loading resolves environment and secret references, so referenced variables must be set even for validation.

## Stubbed application commands

Modules for `doctor`, `ingest`, `retrieve`, and `status` exist under `harborrag_app/cli/commands/`, but they currently contain implementation TODOs and are not registered with the parser. Only `doctor` and `sample-ingest` above are runnable.

The CLI always exits zero after a successfully dispatched current command; structured nonzero exit handling for future operational commands is not implemented.
