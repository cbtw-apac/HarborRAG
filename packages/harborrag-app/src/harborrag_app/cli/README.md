# HarborRAG CLI

The `harborrag_app.cli` package is the operator-facing command-line boundary.
It renders workflow and retrieval results, but delegates all operations to the
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
- `retrieve` requires the configured embedding provider, Qdrant, FalkorDB,
  and the ingestion object store;
- production control-plane composition requires its SQL database.

The repository development topology can be started with:

```bash
scripts/deployment/dev_up.sh --detach
```

## Command overview

| Command | Purpose |
| --- | --- |
| `harborrag doctor` | Check Temporal connectivity and readiness |
| `harborrag ingest start` | Submit a durable ingestion run |
| `harborrag ingest status` | Read current progress and attention queues |
| `harborrag ingest wait` | Wait for the terminal result |
| `harborrag ingest watch` | Open the interactive Textual dashboard |
| `harborrag ingest pause` | Request a durable pause |
| `harborrag ingest resume` | Resume a paused run |
| `harborrag ingest cancel` | Cancel gracefully or immediately |
| `harborrag ingest retry` | Retry selected failed artifacts |
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

## Ingestion

Submit a bounded local-connector run:

```bash
harborrag ingest start \
  --tenant tenant-1 \
  --connector local \
  --limit 100
```

Omit `--limit` to process every discovered artifact. Run, manifest, and index
generation IDs are generated when omitted:

```bash
harborrag ingest start \
  --tenant tenant-1 \
  --connector local \
  --run-id release-notes-2026-07 \
  --manifest-id manifest-2026-07 \
  --generation-id generation-2026-07 \
  --wait
```

`--wait` keeps the command attached until Temporal returns the terminal run
summary. The connector name must match an enabled connector in the worker's
configuration.

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
harborrag ingest cancel RUN_ID --force
harborrag ingest retry RUN_ID \
  --artifact ARTIFACT_ID \
  --artifact ANOTHER_ARTIFACT_ID
```

Cancellation is graceful by default so the workflow can reconcile state.
`--force` requests immediate cancellation. Retry requires at least one
repeatable `--artifact` option.

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
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models.yaml` | Query embedding configuration |
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
