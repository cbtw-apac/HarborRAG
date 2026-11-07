# Troubleshooting


## Projects created with `harborrag init`

Start with `harborrag doctor`: each ✗ line prints its fix, and the
[Quick Start](../../getting-started/quick-start.md#4-check-everything-is-in-place)
tabulates the common ones (blank API key, services not started, missing clients, wrong
source folder, blank connector credentials, running outside the project).

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
| Connector example fails to load | Documentation path was copied without the `.example` suffix | Load or copy `config/sources/connectors.example.yaml` |
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
- Temporal starts but no HarborRAG worker appears: run
  `scripts/deployment/dev.sh worker`.
- `harborrag` is not found: use `uv run harborrag` from the
  workspace or reinstall `harborrag-app` so its console script is registered.
- An ingestion command cannot connect: set `HARBORRAG_TEMPORAL_TARGET` to a
  reachable Temporal frontend (normally `localhost:7233` from the host).

### An accepted ingestion appears to do nothing

Read the task resource first. A `202 Accepted` response means the workflow was
submitted; processing continues asynchronously:

```bash
curl --fail-with-body \
  http://127.0.0.1:8000/v1/admin/ingestions/INGESTION_ID
```

`discovered > 0` with `processed = 0` can mean the first bounded document
window is still running. It is not evidence that the worker is idle. Follow the
worker logs and look for the same `task_id`, `workflow_id`, stage, and safe
error code:

```bash
docker logs --follow harborrag-temporal-temporal-worker-1
```

At `HARBORRAG_LOG_LEVEL=INFO`, HarborRAG logs API submissions, source discovery,
document outcomes, safe activity failures, and finalization. `DEBUG` also logs
each activity start/completion and its duration. Every record includes the
logger namespace, Python module, function, and source line. Logs intentionally
exclude connector credentials, raw document content, prompts, and model output.

If the worker reports that previous chunk or representation artifacts are
missing during a metadata-only update, current releases fall back to fresh
encoding. This reuse path is an optimization and must not fail the ingestion.
For a task created by an older worker, allow it to finish, deploy the corrected
worker, and use `POST /v1/admin/ingestions/{ingestion_id}/retry-failures` for its retryable
document failures.

### A stopped ingestion still answers `409 already_running`

Stop an ingestion with `POST /v1/admin/ingestions/{ingestion_id}/cancel`, not
with Terminate in the Temporal UI. A terminated workflow never records its end,
so the run stays `RUNNING`. HarborRAG checks Temporal before it refuses a start:
a run whose workflow ended is recorded as `CANCELLED` (terminated or cancelled)
or `FAILED` (failed, timed out, or completed unrecorded), and the start goes
ahead. Removing the connection or cancelling the run does the same; until one of
these happens, the run still reads `RUNNING`. A run Temporal reports as running
or paused, or cannot describe, such as a terminated retry run, still blocks its
connection.

## Chat and MCP

Chat is HTTP/CLI-only; MCP exposes only retrieval tools.

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| Chat validation reports a missing `HARBORRAG_CHAT_*` value | `config/models/models.yaml` expands provider settings before the first call | Populate and load `env/.env.models`, or inject the same values through the deployment secret manager |
| HTTP chat returns `503` | Model configuration, credentials, provider reachability, or a provider limit failed behind the normalized API boundary | Inspect server logs and validate the `chat` family; provider details are intentionally not returned to callers |
| MCP appears to do nothing when started in a terminal | Stdio MCP waits for a client protocol over pipes and has no port or interactive prompt | Run `scripts/deployment/mcp.sh --check`, or use `--http` and open `http://127.0.0.1:8010/` |
| The browser cannot load or run tools | Missing/wrong owner token or the HTTP server is not running | Load `HARBORRAG_MCP_BEARER_TOKEN` from the ignored `env/.env.mcp`; never paste model API keys into the UI |
| MCP accepts the bearer token but retrieval is denied or empty | The token authenticates the client; `source_acl` remains the default reader policy, or the tool call names a different tenant | For a deliberately shared corpus, set `HARBORRAG_MCP_READER_TENANT_ID`, `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared`, and `HARBORRAG_CORPUS_SHARED_TENANT_ID` to the same tenant, then restart MCP. For restricted corpora, import current source/document ACLs instead. |
| A tool change is saved but clients still list the old schema | FastMCP snapshots globally advertised schemas at startup | Restart when the UI reports `restart_required=true` |

The MCP configuration UI controls enablement, defaults, numeric limits, and
tenant overrides in `config/mcp/server.yaml`. It intentionally cannot change provider
credentials, provider endpoints, or stored prompt files. See
[MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).

## Unexpected logs or oversized embedding input

An error such as `Too many input tokens. Max input tokens: 8192` means the
text sent to the embedding provider exceeded that model's context window. A
large character limit or file-size limit does not guarantee a safe token count,
especially for timestamps, stack traces, escaped JSON, ANSI sequences, and
other dense log syntax.

HarborRAG stores source content because the selected connector admitted it;
the ingestion pipeline does not classify timestamped or error-like text as
disposable system output. If logs are not knowledge sources, exclude them in
the connector before reingestion:

```yaml
settings:
  excluded_extensions: [log]
  exclude_paths: [logs]
  exclude_globs: ["*.log", "**/*.log", "**/logs/**", "**/*.jsonl"]
```

If logs are intentional knowledge sources, use a tokenizer-aware chunking
policy whose input allowance is below the embedding model's maximum, leaving
room for provider-added formatting. Reingest after changing source filters or
chunking; failed oversized chunks are not repaired by increasing
`max_tokens`, which controls chat output rather than embedding input.

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

The API stops at startup with `control-plane database is at schema revision X,
newer than this build's head Y` when a newer build (another branch or image)
already migrated the same control database. Run that newer build, or back up the
database and downgrade it to `Y` with the newer build's migrations; this build
cannot do it, because it does not ship the newer revisions. To keep builds from
migrating each other's schema, give each one its own `POSTGRES_DB`.
