# harborrag-app

The `harborrag` operator command and the HarborRAG HTTP API. Use it to start, follow and
control ingestion runs, manage connections, search the indexes and ask grounded questions,
from a terminal or over HTTP.

## Install

Run the CLI from inside a HarborRAG repository checkout. The
[Quick Start](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/quick-start.md)
takes you from `git clone` to a first answer: it installs the CLI with
[uv](https://docs.astral.sh/uv/), starts the local stack and runs a first ingestion.

## Quick start

From the checkout, list the commands:

```bash
uv run harborrag --help
```

You should see eight commands, grouped by purpose: `doctor`, `auth` and `config`
(Operations); `chat` (Chat); `retrieve` (Retrieval); and `ingest`, `topology` and `source`
(Ingestion). `uv run harborrag COMMAND --help` lists each command's options.

## What it provides

- **The `harborrag` CLI.** Every command and option is in the
  [CLI reference](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md).
- **The HTTP API** under `/v1`: connections, projects, runtime settings, ingestion runs
  and the failure inbox under `/v1/admin`, chat sessions and completions under `/v1/chat`.
  See [Connections](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/connections.md)
  and [Chat](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md).
- **One application service** that both call. The CLI and the API never construct
  connectors or repositories themselves.

## Run and control ingestions

`ingest run` executes an ingestion in the current process, without Temporal or a worker.
`ingest start` submits it to Temporal, and the other actions follow or control the run:

```bash
uv run harborrag ingest start harborrag-workspace --wait
uv run harborrag ingest status RUN_ID --json
uv run harborrag ingest watch RUN_ID
uv run harborrag ingest pause RUN_ID
uv run harborrag ingest resume RUN_ID
uv run harborrag ingest cancel RUN_ID
```

The argument is the connection's `source_key`, not a source type such as `local`; how that
key is formed is in
[Name the connector](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#name-the-connector).
`uv run harborrag source sync PROJECT/CONNECTION` starts any connection by project and
connection ID, whatever its key. From a checkout, commands that write need the stack's
control database first; see
[Run from a repository checkout](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#run-from-a-repository-checkout).

`ingest watch` and `ingest start --wait` follow a run with an inline progress block.
Every command that takes `--json` prints one stable envelope,
`{"ok":...,"data":...,"error":...}`, without color or spinners; see
[Read JSON output and exit codes](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#read-json-output-and-exit-codes).
The durable workflows behind `ingest start` are described in
[Runtime reliability](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/developers/architecture/runtime-reliability.md).

## Schedule ingestions

Schedules run in Temporal and are declared in `config/sources/schedules.yaml`, which the
API applies each time it starts. See
[Scheduled ingestion](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/scheduled-ingestion.md)
for the file format, pausing a schedule during an incident, and monitoring.

## Search the indexes

`harborrag retrieve` searches the active Qdrant vectors of a tenant and expands the leading
results with graph context from FalkorDB:

```bash
uv run harborrag retrieve "deployment requirements" --top-k 5
uv run harborrag retrieve "deployment requirements" --lane sparse --no-graph --json
```

Retrieved text is left out unless you pass `--include-content`, so shell logs and
automation artifacts do not collect source content. An unexpected provider error reaches
the output only as its exception type, never its message. From a checkout, retrieval also
needs the two variables in
[Retrieve evidence](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#retrieve-evidence).

Retrieval is served by this CLI, the
[MCP tools](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/README.md)
and chat (`POST /v1/chat/completions`); the HTTP API has no separate retrieval routes.

## Contributing

Tests for this package live in `packages/harborrag-app/tests/`. Keep new tests there when
you add or change behaviour this package owns, and run them from the repository root:

```bash
uv run pytest packages/harborrag-app/tests
```

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md) for the
contribution workflow.

## License

Apache-2.0. See [LICENSE](https://github.com/cbtw-apac/HarborRAG/blob/main/LICENSE).
