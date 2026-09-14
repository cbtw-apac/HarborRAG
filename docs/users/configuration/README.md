# Configuration

## If you created your project with `harborrag init`

Everything lives in the project folder and you rarely need more than two files:

| To change… | Edit | Then |
| --- | --- | --- |
| your API key, a service URL, a source folder or token | `.env` | `harborrag doctor` |
| which sources are ingested | `config/connectors.yaml` (+ the `${VARIABLES}` it names, in `.env`) | `harborrag doctor`, then `harborrag ingest run NAME` |
| the chat or embedding model | `config/models.yaml` | `harborrag doctor`; re-ingest with `--force-reprocess` if the *embedding* model changed |
| how PDFs, Office files and images are parsed | `config/parsers.yaml` | re-ingest the affected files |

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

## The four configuration files

HarborRAG has four independent configuration paths:

1. [Connector Configuration](connector-config.md) - versioned YAML loaded by `harborrag-runtime`.
2. [Parser Configuration](parser-config.md) - versioned YAML loaded by `harborrag-runtime`.
3. [Model Configuration](model-config.md) - YAML or JSON loaded by the chat, embedding, and reranking clients in `harborrag-adapters`.
4. [Temporal Configuration](../../../deploy/temporal/README.md) - versioned YAML for connection policy, workers, queues, retries, and workflow timeouts.

[Engine Configuration](config-file-reference.md) documents the small code-constructed engine dataclasses. [Tenant and Workspace Status](workspace-mode.md) explains tenant-aware repository context and what is not yet available as a workspace feature.

For the composed chat surfaces and stored prompt catalog, see
[Chat](../chat/README.md). For MCP tool defaults, limits, tenant overrides, and
the authenticated configuration UI, see
[MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).

The checked-in `*.example.yaml` and `.env.*.example` files in the repository are references, not automatically loaded runtime files. Copy them for an environment or pass their paths explicitly. Outside a scaffolded project the CLI loads only the checkout's `env/.env.connector`, `env/.env.parser` and `env/.env.models`; inside one it loads the project `.env`.
