# HarborRAG CLI

The `harborrag_app.cli` package is the operator-facing command-line boundary.
It renders workflow, retrieval, and chat results, but delegates all operations to the
same transport-neutral application service used by the Control Plane API.

The installed command is `harborrag`.

## Install and run

From a source checkout, install the production runtime dependencies required
by ingestion and hybrid retrieval:

```bash
uv sync --package harborrag-app --extra production
uv run --package harborrag-app harborrag --help
```

The base package is sufficient for help and presentation code. Live commands
also require their configured services:

- `doctor` and `ingest` require Temporal;
- `chat` requires the configured chat provider and model credentials;
- `retrieve` requires the configured embedding provider, Qdrant, FalkorDB,
  and the ingestion object store;
- production control-plane composition requires its SQL database.

The repository development topology can be started with:

```bash
scripts/deployment/dev.sh up
```

Build and run the non-root CLI image from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.cli -t harborrag-cli .
docker run --rm \
  --env-file env/.env.models \
  harborrag-cli chat "Explain HarborRAG" --json
```

The image contains the tracked configuration and prompt templates. Mount
`config/` at `/app/config:ro` when an operator-managed configuration should
replace the image copy. Database, Temporal, and retrieval commands additionally
need their service environment variables and network connectivity.

## Command overview

| Command | Purpose |
| --- | --- |
| `harborrag doctor` | Check Temporal connectivity and readiness |
| `harborrag chat` | Generate a retrieval-grounded response with conversation memory |
| `harborrag ingest start` | Submit a durable ingestion run |
| `harborrag ingest status` | Read current progress and attention queues |
| `harborrag ingest wait` | Wait for the terminal result |
| `harborrag ingest watch` | Open the interactive Textual dashboard |
| `harborrag ingest pause` | Request a durable pause |
| `harborrag ingest resume` | Resume a paused run |
| `harborrag ingest cancel` | Cancel at a safe workflow boundary |
| `harborrag retrieve` | Run tenant-scoped Qdrant and FalkorDB retrieval |

Use `harborrag COMMAND --help` or
`harborrag ingest ACTION --help` for the authoritative option list.

## Diagnostics

```bash
harborrag doctor
harborrag doctor --json
```

`doctor` connects to the configured Temporal frontend and reports its target,
namespace, and readiness. It does not submit a workflow.

## Chat

```bash
harborrag chat \
  "Explain HarborRAG" \
  --tenant DEFAULT \
  --json
```

The command uses the server-owned default prompt and retrieval-grounded chat
service. Omit `--session` for the first turn; the JSON response contains the
generated session ID. Reuse it with `--session` to recall the two latest
PostgreSQL-backed turns.

## Ingestion

`--connector` takes a **configured connector name** — a key under `connectors:`
in `config/connectors.yaml` — not a provider type. The shipped configuration
defines `harborrag-workspace` (provider `local`), `confluence-main`, and
`jira-main`, so `--connector local` fails with
`Unknown configured connector: 'local'`. List the configured names with:

```bash
python -c "import yaml; print(*yaml.safe_load(open('config/connectors.yaml'))['connectors'])"
```

Each connector resolves settings and credentials from the environment variables
named in its `environment:` and `secrets:` blocks. Every one of them must be set
before the run is accepted:

| Connector | Required environment |
| --- | --- |
| `harborrag-workspace` | `LOCAL_SOURCE_PATH` |
| `confluence-main` | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_EMAIL`, `CONFLUENCE_TOKEN` |
| `jira-main` | `JIRA_BASE_URL`, `JIRA_PROJECT_KEY`, `JIRA_TOKEN`, `JIRA_EMAIL` |

**You do not need to set these on the command line.** The CLI loads
`env/.env.connector`, `env/.env.parser`, and `env/.env.models` on startup — the
same files compose hands to the worker — so credentials configured once are
picked up by every command. An exported or inline variable still wins over the
file. `env/.env.api` and `env/.env.database` are deliberately not loaded: they
point at in-cluster hostnames a host CLI cannot reach. `CONNECTOR_ENV_FILE`,
`PARSER_ENV_FILE`, and `MODEL_ENV_FILE` relocate the files, exactly as in
`scripts/deployment/dev.sh`.

A missing variable fails fast and names both the variable and the field it feeds:
`Connector 'jira-main' requires environment variable 'JIRA_BASE_URL' for 'base_url'`.

With `env/.env.connector` populated, a run is just:

```bash
harborrag ingest start \
  --tenant tenant-1 \
  --connector-id jira-main \
  --limit 100
```

`--connector-id` and `--connector` are the same option. Do not confuse either with
`--connection-id`, which sets the stable logical connection identity and defaults to
the connector name.

The local connector works the same way:

```bash
harborrag ingest start \
  --tenant tenant-1 \
  --connector-id harborrag-workspace \
  --limit 100
```

`JIRA_PROJECT_KEY` accepts a comma-separated list (`PROJ,OPS`). `JIRA_TOKEN` is an
Atlassian API token, and on Cloud it is paired with `JIRA_EMAIL` for basic auth.

> Credentials must also be present in the **worker**, which runs in its own
> container and does the actual fetching. Compose supplies them from
> `env/.env.connector` via `env_file:`, so editing that one file covers both the CLI
> and the worker — but a worker started before the edit keeps the old values until
> it is restarted.

> **A zero exit from `ingest start` means the workflow was submitted, not that it
> succeeded.** Credentials and connectivity are only exercised once the run reaches
> its Fetch stage, so a bad token still submits cleanly and fails afterwards. Use
> `--wait`, or check `harborrag ingest status <run-id>`, before treating a run as
> done.

Omit `--limit` to process every discovered document. A run ID is generated
when omitted. Connection and source-scope IDs are deterministic unless supplied
explicitly:

```bash
harborrag ingest start \
  --tenant tenant-1 \
  --connector harborrag-workspace \
  --run-id release-notes-2026-07 \
  --connection-id engineering-files \
  --source-scope-id engineering-release-notes \
  --pattern '*.md' \
  --no-attachments \
  --wait
```

`--wait` keeps the command attached until Temporal returns the terminal run
summary. The connector name must match an enabled connector in the worker's
configuration.

### Schema errors during ingestion

Commands that do real work refuse to start when the control-plane database is not
usable, and say why:

```
✗ Control plane is not ready: migrations failed: (sqlite3.OperationalError)
  table projects already exists. Refusing to run against a database whose
  schema may be stale; run 'harborrag doctor' for diagnostics.
```

`harborrag doctor` is exempt — it stays available precisely when the control plane
is degraded. With `--json`, the full underlying error is in `data.detail`.

If you reach a missing-column error instead (for example
`no such column: source_scopes.tenant_id`), the migrations did not run. Boot only
logs that and continues, so check the startup line:

```
ERROR harborrag.runtime.composition Control-plane migrations failed ... error=...
```

`table <name> already exists` there means the schema was created without Alembic
recording it, so the runner replays from the first revision and collides. Stamp
the version table at the revision the schema already matches, then upgrade:

```bash
python - <<'PY'
from alembic import command
from harborrag_adapters.repositories.database.control_plane import migrations
cfg = migrations._build_config("sqlite+aiosqlite:///./harborrag_control.db")
command.stamp(cfg, "0007")   # the revision the existing schema matches
command.upgrade(cfg, "head")
PY
```

### Observe a run

```bash
harborrag ingest status RUN_ID
harborrag ingest wait RUN_ID
harborrag ingest watch RUN_ID --refresh 1.5
```

- `status` performs one non-blocking status query;
- `wait` blocks for the final result;
- `watch` opens a full-screen dashboard and polls every second by default.

The dashboard refresh interval accepts values from 0.25 through 60 seconds.
Its footer exposes refresh, pause, resume, cancellation, and quit controls.
See [dashboard/README.md](dashboard/README.md) for the presentation boundary.

### Control a run

```bash
harborrag ingest pause RUN_ID
harborrag ingest resume RUN_ID
harborrag ingest cancel RUN_ID
```

Cancellation waits for a safe source-batch boundary, persists `CANCELLED`, and
drains eligible projection cleanup jobs. Activity retries are automatic. To
replay a terminal failure, submit a new run for the same source scope; the
pipeline resumes from raw, canonical, chunk, or projection artifacts when
their fingerprints remain reusable.

## Hybrid retrieval

```bash
harborrag retrieve \
  "deployment requirements" \
  --tenant tenant-1 \
  --top-k 5
```

Retrieval:

1. embeds the query using the configured query embedding deployment;
2. searches active, tenant-scoped Qdrant vectors;
3. expands matching chunk context through FalkorDB;
4. fuses vector and graph rankings;
5. loads canonical chunk text from the ingestion object store.

Document content is omitted from output by default:

```bash
harborrag retrieve \
  "deployment requirements" \
  --tenant tenant-1 \
  --top-k 5 \
  --include-content
```

Only use `--include-content` when the terminal and downstream consumers are
authorized to receive source text. The CLI process must point at the same
object-store root, active index generation, collection, graph namespace, and
embedding space used by the ingestion worker.

## Machine-readable output

Every one-shot command supports `--json`:

```bash
harborrag ingest status RUN_ID --json
harborrag chat "Explain HarborRAG" --json
harborrag retrieve "deployment requirements" \
  --tenant tenant-1 \
  --json |
  jq .
```

JSON mode writes exactly one stable envelope to stdout:

```json
{"data":{},"error":null,"ok":true}
```

Logs remain on stderr, and Rich spinners, panels, and ANSI styling are
suppressed. Successful operations exit with status 0; validation and runtime
failures exit with a non-zero status.

For human-readable commands, disable color globally:

```bash
harborrag --no-color ingest status RUN_ID
NO_COLOR=1 harborrag ingest status RUN_ID
```

Set `HARBORRAG_LOG_LEVEL=INFO` or `DEBUG` for diagnostic logs. The CLI defaults
to `WARNING` so normal and JSON output remain concise.

## Runtime configuration

The CLI constructs production application composition for every invocation.
Relevant `HARBORRAG_` variables include:

| Variable | Default | Used by |
| --- | --- | --- |
| `HARBORRAG_ENV` | `dev` | Runtime safety policy |
| `HARBORRAG_CONTROL_DB_URL` | local SQLite | Control-plane migrations and diagnostics |
| `HARBORRAG_TEMPORAL_TARGET` | `localhost:7233` | `doctor` and all ingestion commands |
| `HARBORRAG_TEMPORAL_NAMESPACE` | `harborrag` | Temporal workflow lookup |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models.yaml` | Chat and query embedding configuration |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` | Vector retrieval |
| `HARBORRAG_FALKORDB_HOST` | `localhost` | Graph expansion |
| `HARBORRAG_FALKORDB_PORT` | `6379` | Graph expansion |
| `HARBORRAG_INGESTION_OBJECT_ROOT` | `.harborrag/objects` | Canonical retrieved chunk content |
| `HARBORRAG_VECTOR_COLLECTION` | `harborrag_chunks` | Vector collection |
| `HARBORRAG_GRAPH_NAMESPACE` | `harborrag` | Graph projection namespace |
| `HARBORRAG_LOG_LEVEL` | `WARNING` for CLI | Diagnostic logging |

Plaintext connections to non-loopback services fail closed unless their
explicit `HARBORRAG_*_ALLOW_INSECURE_REMOTE` development-network option is
enabled. Do not use those opt-outs on an untrusted network.

### The object root must be shared with the Temporal worker

Qdrant stores vectors and chunk *references*; the canonical chunk bodies live
on the filesystem under `HARBORRAG_INGESTION_OBJECT_ROOT`. `retrieve` reads
those bodies, so it must point at the same object root the worker wrote them
to. The Compose worker sets `/var/lib/harborrag/objects` (Docker volume
`harborrag-ingestion-data`), while a CLI run on the host defaults to
`./.harborrag/objects`.

When the two disagree, ingestion reports success and retrieval then fails with:

```text
HarborStorageNotFoundError: object ingestion/chunks/<sha>.json does not exist
```

even though the vectors are present. Point the CLI at the worker's object root
— the directory *containing* `ingestion/`, not `ingestion/` itself — or run
`retrieve` inside the worker's environment.

## Package boundary

This package may:

- define Typer commands and validate command-line arguments;
- render `AppResponse` values with Rich or Textual;
- manage one application-service lifecycle per invocation;
- emit stable JSON and exit codes for automation.

It must not:

- call connectors, parsers, repositories, or Temporal SDK handles directly;
- implement workflow or retrieval business rules;
- import FastAPI to provide CLI behavior;
- expose secret or unreviewed provider exception text.

Run its focused tests from the repository root:

```bash
pytest \
  packages/harborrag-app/tests/test_ingestion_cli_rendering.py \
  packages/harborrag-app/tests/test_ingestion_service_cli.py \
  packages/harborrag-app/tests/test_retrieval_cli.py
```
