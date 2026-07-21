# Real smoke-test runbook

These manual, opt-in checks answer one question: can an installed adapter
complete a small operation against a real dependency? They do not use pytest,
mocks, monkeypatching, recorded responses, or fake provider clients.

Smoke checks may consume paid API quota, access private content, download
models, or require services and network routes that are unavailable in a
developer environment. An unavailable target should return exit code `2`; do
not replace its real dependency with a mock just to make it pass locally.

## Module guides

Each smoke module owns its setup, commands, success criteria, and
troubleshooting guide:

| Module | Guide | Scope |
| --- | --- | --- |
| Connectors | [connectors/README.md](connectors/README.md) | Local, Confluence, GitHub, JIRA, and SharePoint discovery/load checks |
| Models | [models/README.md](models/README.md) | Live chat, embedding, and reranking requests |
| Parsers | [parsers/README.md](parsers/README.md) | Real local documents and selectable PDF engines/profiles |
| Repositories | [repositories/README.md](repositories/README.md) | SQLite, PostgreSQL, Redis, Qdrant, and FalkorDB operations |

Run one target from its module first. Use that module's `run_all.py` only after
the individual targets you configured succeed. The parser module intentionally
has one `parse_file.py` entry point instead of a group runner.

## Shared environment behavior

Connector, model, and repository scripts load configuration in this order:

1. Variables already exported by the process.
2. The dotenv file selected by `HARBOR_SMOKE_ENV_FILE`.
3. The repo-root `.env` when no explicit file is selected.

Exported variables are never overwritten. Parser smoke checks take their input
and PDF selection from command-line arguments and do not load a dotenv file.

Use the repo-root examples as references:

- `env-example/.env.connector.example`
- `env-example/.env.models.example`
- `env-example/.env.database.example`
- `env-example/.env.parser.example`

Keep populated files untracked. A protected file can be selected for one
command:

```bash
HARBOR_SMOKE_ENV_FILE=/secure/path/harbor-smoke.env \
  python packages/harborrag-adapters/tests/smoke/connectors/github.py
```

Run commands from the repository root with Python 3.12. Each module guide lists
the relevant `uv sync` or optional-dependency command.

## Shared safety rules

1. Use least-privilege test tenants, repositories, spaces, projects, drives,
   databases, and provider keys.
2. Review provider pricing, quota, and model-download requirements before a
   model, OCR, or PDF-engine run.
3. Never commit dotenv files or capture authorization headers, raw provider
   payloads, prompts, responses, extracted document text, or embedding vectors.
4. Connector output is metadata-only by default. `HARBOR_SMOKE_VERBOSE=1`
   enables bounded, redacted previews locally; previews remain disabled in CI.
5. Use disposable repository targets. Confirm cleanup behavior in the
   repository module guide before pointing a check at a service.

## Exit codes

| Code | Meaning | Action |
| --- | --- | --- |
| `0` | The configured real operation passed | Record sanitized evidence if required |
| `1` | Configuration existed, but the operation or invariant failed | Investigate the sanitized failure |
| `2` | The target, dependency, credential, or real input is unavailable | Configure it later or record it as pending |

Group runners may have module-specific handling for unavailable targets; see
the corresponding module README.

## Release evidence

Record only the command, exit code, UTC timestamp, adapter commit, selected
provider/backend, dependency versions, safe counts, and latency. Review output
before sharing it. Never attach secret files or source/provider content.
