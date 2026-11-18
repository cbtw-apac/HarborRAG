# harborrag-app

The `harborrag` operator command and the HarborRAG HTTP API. Use it to manage projects
and connections, start, follow and control ingestion runs, change runtime settings and ask
grounded questions, from a terminal or over HTTP.

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

You should see the commands grouped by purpose: `me` and `doctor` under
Setup and diagnostics; `auth` under Access; `chat` under Chat; and `projects`,
`source-types`, `connections`, `ingestions`, `failures` and `settings` under Resources.
`uv run harborrag COMMAND --help` lists each command's options.

## What it provides

- **The `harborrag` CLI.** Each resource command calls the same operation as its HTTP
  route, with the same input, output and error codes; `doctor`, `auth keys` and
  `settings import|export` are CLI-only and call no HTTP operation. The
  [CLI reference](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md)
  maps them, and the
  [CLI command reference](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/commands.md)
  lists every option.
- **The HTTP API** under `/v1`: connections, projects, runtime settings, ingestion runs
  and the failure inbox under `/v1/admin`, chat sessions and completions under `/v1/chat`.
  See [Connections](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/connections.md)
  and [Chat](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md).
- **One set of operations** that both call, held in `operations/`. The CLI and the API
  never construct connectors or repositories themselves.

The package has three parts:

- `api/` holds the HTTP routes, the authentication dependencies, the error handlers and the
  stream deadline and heartbeat of streamed responses.
- `operations/` holds the port both transports call (`port.py`), the table of every
  operation (`contract.py`), the request and response models (`schemas/`), the `Principal`,
  SSE event framing (`sse.py`), the readiness checks and the in-process implementation
  (`local/`, which covers chat, ingestions, settings and admin operations).
- `cli/` holds the commands and the kernel that runs them.

The application services behind the operations live in
[`harborrag-runtime`](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-runtime/src/harborrag_runtime/application/README.md);
the retrieval, ingestion and topology use cases they run live in `harborrag-engine`, and the
conversation memory use cases live in `harborrag-memory`.

## Run and control ingestions

`ingestions start` submits an ingestion of a connection to Temporal, as
`POST /v1/admin/ingestions` does; `--wait` follows it until it ends, and the other
commands read or control the run by its `ingestion_id`:

```bash
uv run harborrag ingestions start default/harborrag-workspace --wait
uv run harborrag ingestions list --connection-ref default/harborrag-workspace
uv run harborrag ingestions get INGESTION_ID --watch
uv run harborrag ingestions pause INGESTION_ID
uv run harborrag ingestions resume INGESTION_ID
uv run harborrag ingestions cancel INGESTION_ID
```

The argument of `start` is the connection's ref, `PROJECT_ID/CONNECTION_ID`;
`uv run harborrag connections list` lists them. `--inline` runs the ingestion in the
current process, without Temporal or a worker; it reads files on this machine, so it
refuses a `local_file` connection whose `source_path` is not a directory here, such as
the stack's `/data/sources`. See
[Run an ingestion in this process](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#run-an-ingestion-in-this-process).
From a checkout, commands that write need the stack's control database first; see
[Run from a repository checkout](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#run-from-a-repository-checkout).

`-o json` prints the HTTP response body, and an error goes to stderr as the HTTP error
envelope, with an exit code for each class of status, and `8` after the body when a
followed run or a `connections test` failed; see
[Read errors and exit codes](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#read-errors-and-exit-codes).
The durable workflows behind `ingestions start` are described in
[Runtime reliability](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/developers/architecture/runtime-reliability.md).

## Schedule ingestions

Schedules run in Temporal and are declared in `config/sources/schedules.yaml`, which the
API applies each time it starts. See
[Scheduled ingestion](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/scheduled-ingestion.md)
for the file format, pausing a schedule during an incident, and monitoring.

## Ask a grounded question

`harborrag chat` calls `POST /v1/chat/completions`: it retrieves the passages that match a
question and answers with numbered citations.

The CLI reads the corpus mode from the API's env file, so it answers from the same
documents as HTTP chat. The shipped `env-example/.env.api.example` sets
`HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`,
and from a checkout the CLI reads exactly those two lines of `env/.env.api` (the file named
by `API_ENV_FILE` when you set it), and nothing else in that file. An exported variable
outranks the file. Remove both lines to return the tenant to `source_acl`, where a caller
with no source permissions gets no sources; an `env/.env.api` created from an older
template has neither line, so add them and restart the API.

Ask:

```bash
uv run harborrag chat "Explain HarborRAG in one paragraph." -o json
```

The
[CLI reference](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#ask-a-grounded-question)
has the options.
Search without a chat model is served by the
[MCP tools](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/README.md);
the HTTP API has no separate retrieval routes.

## Contributing

Tests for this package live in `packages/harborrag-app/tests/`. Keep new tests there when
you add or change behaviour of the CLI, the HTTP API or the operations layer, and run them
from the repository root:

```bash
uv run pytest packages/harborrag-app/tests
```

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md) for the
contribution workflow.

## License

Apache-2.0. See [LICENSE](https://github.com/cbtw-apac/HarborRAG/blob/main/LICENSE).
