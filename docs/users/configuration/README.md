# Configuration

## If you created your project with `harborrag init`

Everything lives in the project folder and you rarely need more than two files:

| To change… | Edit | Then |
| --- | --- | --- |
| your API key, a service URL, a source folder or token | `.env` | `harborrag doctor` |
| which sources are ingested | `config/sources/connectors.yaml` (+ the `${VARIABLES}` it names, in `.env`) | `harborrag doctor`, then `harborrag ingest run NAME` |
| the chat or embedding model | `config/models/models.yaml` | `harborrag doctor`; re-ingest with `--force-reprocess` if the *embedding* model changed |
| how PDFs, Office files and images are parsed | `config/ingestion/parsers.yaml` | re-ingest the affected files |
| chunk sizes, per-source profiles, how oversized code and prose are split | `config/ingestion/chunking.yaml` | `harborrag doctor`; documents re-chunk on their next ingestion |

Rules that keep this simple:

- **Secrets and machine-specific values go in `.env`; the YAML files only reference them**
  as `${NAME}`. The CLI loads the project's `.env` automatically; the shell environment
  always wins over it, and `.env` wins over the `runtime:` defaults in `harborrag.yaml`.
- **Nothing is picked up silently.** Every command finds the project by walking up from your
  current directory to `harborrag.yaml` (or via `--project DIR`), and only trusts a folder
  you own. `harborrag doctor` validates all four files and names any blank variable.
- **Re-running `harborrag init`** inside the project asks before overwriting the YAML files
  and never overwrites `.env` (a fresh copy goes to `.env.new`).

The rest of this page describes each file in depth and how the repository checkout wires
the same files through Compose.

## The `config/` folder

The checkout keeps one folder per domain. Only the platform-wide files sit at the root:

| Folder | Files | Page |
| --- | --- | --- |
| `config/` | `settings.example.yaml` (the keys the git-ignored `settings.yaml` may set), `temporal.yaml` | [Runtime Settings](runtime-settings.md), [Temporal Configuration](../../../deploy/temporal/README.md) |
| `config/sources/` | `connectors.yaml`, `connectors.example.yaml`, `schedules.example.yaml` (copy to the git-ignored `schedules.yaml`) | [Connector Configuration](connector-config.md), [Scheduled Ingestion](../scheduled-ingestion.md) |
| `config/ingestion/` | `parsers.yaml`, `chunking.yaml` | [Parser Configuration](parser-config.md), [Chunking Configuration](chunking-config.md) |
| `config/graph/` | `build.yaml`, `ontologies/example.yaml` | [Graph Build Configuration](graph-build-config.md) |
| `config/models/` | `models.yaml`, `models.production.example.yaml` | [Model Configuration](model-config.md) |
| `config/mcp/` | `server.yaml`, `keys.example.yaml` (copy to the git-ignored `keys.yaml`) | [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md) |

The YAML files hold values only; each page explains them. `config/ingestion/parsers.yaml`
is the exception: its inactive alternatives stay as commented blocks, because its bytes are
part of the processing identity. Each file's location is a setting, such as
`HARBORRAG_PARSER_CONFIG_PATH` or `HARBORRAG_MCP_KEYS_PATH`, so a deployment can keep its
files elsewhere. Compose mounts the whole folder read-only into the
API, the worker and the MCP server.

[Runtime Settings](runtime-settings.md) lists the six settings an administrator changes through the API without a restart.

[Engine Configuration](config-file-reference.md) documents the small code-constructed engine dataclasses. [Tenant and Workspace Status](workspace-mode.md) explains tenant-aware repository context and what is not yet available as a workspace feature.

For the composed chat surfaces and stored prompt catalog, see
[Chat](../chat/README.md).

The checked-in `*.example.yaml` and `.env.*.example` files in the repository are references, not automatically loaded runtime files. Copy them for an environment or pass their paths explicitly. Outside a scaffolded project the CLI loads only the checkout's `env/.env.connector`, `env/.env.parser` and `env/.env.models`; inside one it loads the project `.env`.
