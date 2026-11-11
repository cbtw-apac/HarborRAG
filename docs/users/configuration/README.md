# Configuration

This section is for anyone who runs HarborRAG and wants to change what it ingests,
which models it calls, or how it parses, chunks and searches documents.

HarborRAG reads its configuration from three places:

- **YAML files under `config/`** say what to do: data sources, parsers, chunking,
  models, the knowledge graph and the MCP tools. They hold values only and name
  secrets by their environment variable instead of containing them.
- **Environment files** hold secrets and service addresses: the git-ignored files in
  `env/` of your repository checkout, which `scripts/deployment/dev.sh bootstrap`
  creates from the templates in `env-example/`.
- **Runtime settings** are six keys an administrator changes while HarborRAG runs,
  stored in the control database, the database where HarborRAG keeps its own state.

## Pages in this section

- [Connector Configuration](connector-config.md) - the data sources in
  `config/sources/connectors.yaml` and the options of each provider.
- [Parser Configuration](parser-config.md) - how PDFs, Office files and images become
  text, and the OCR server.
- [Chunking Configuration](chunking-config.md) - chunk sizes, per-source profiles and
  how oversized code and prose are split.
- [Graph Build Configuration](graph-build-config.md) - entity extraction, summaries
  and per-tenant graph budgets.
- [Model Configuration](model-config.md) - the chat, embedding and reranking models
  and the LiteLLM gateway they go through.
- [Runtime Settings](runtime-settings.md) - the settings you change without a restart,
  over HTTP or with `harborrag config import`.
- [Engine Configuration](config-file-reference.md) - every `HARBORRAG_*` environment
  variable the worker, API and MCP server read.
- [Tenant and Workspace Status](workspace-mode.md) - tenants, projects and their
  limits.

The prompts chat uses are covered in [Chat](../chat/README.md).

## The `config/` folder

A repository checkout keeps one folder per domain. Only the platform-wide files sit at
the root:

| Folder | Files | Page |
| --- | --- | --- |
| `config/` | `settings.example.yaml` (the keys the git-ignored `settings.yaml` may set), `temporal.yaml` | [Runtime Settings](runtime-settings.md), [Temporal Configuration](../../../deploy/temporal/README.md) |
| `config/sources/` | `connectors.yaml`, `connectors.example.yaml`, `schedules.example.yaml` (copy to the git-ignored `schedules.yaml`) | [Connector Configuration](connector-config.md), [Scheduled Ingestion](../scheduled-ingestion.md) |
| `config/ingestion/` | `parsers.yaml`, `chunking.yaml` | [Parser Configuration](parser-config.md), [Chunking Configuration](chunking-config.md) |
| `config/graph/` | `build.yaml`, `ontologies/example.yaml` | [Graph Build Configuration](graph-build-config.md) |
| `config/models/` | `models.yaml`, `models.production.example.yaml` | [Model Configuration](model-config.md) |
| `config/mcp/` | `server.yaml`, `keys.example.yaml` (copy to the git-ignored `keys.yaml`) | [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md) |

The YAML files hold values only; each page explains them. `config/ingestion/parsers.yaml`
is the exception: its inactive alternatives stay as commented blocks, because its bytes
are part of the processing identity. Each file's location is a setting, such as
`HARBORRAG_PARSER_CONFIG_PATH`, so a deployment can keep its files elsewhere
([Engine Configuration](config-file-reference.md)).

Compose mounts the whole folder read-only into the API, the worker and the MCP server.
A process reads its files when it starts, so restart it after an edit; the image needs
no rebuild. A container reads its `env/` files only when it is created, so a changed
environment file needs the container recreated, not just restarted.

The `*.example.yaml` files and the `env-example/` templates are references that nothing
loads: copy them, or pass their paths explicitly.

## Change a setting

| To change | Edit | Then |
| --- | --- | --- |
| The model gateway's URL, key or model names | `env/.env.models` | Recreate the API and worker containers so they read the file again. A new embedding model needs a re-index ([Model Configuration](model-config.md#the-shipped-gateway-catalog)) |
| Which models each family uses, or their provider | `config/models/models.yaml` | Restart the API and the worker |
| A connector token or password | `env/.env.connector` | Recreate the worker container; its next run reads the new value |
| Which sources are ingested, or a source's URL or folder | Before the first start: `config/sources/connectors.yaml` and the variables it names in `env/.env.connector`. After it: `harborrag connections create` or `update`, or the API | [Connections](../connections.md). The CLI writes connections only once `HARBORRAG_CONTROL_DB_URL` points it at the stack's Postgres ([Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout)) |
| How PDFs and images are parsed, and the OCR server | `config/ingestion/parsers.yaml`, `HARBORRAG_OCR_SERVER_URL` in `env/.env.parser` | Restart the API and the worker; the next sync of each connection reprocesses its documents |
| Chunk sizes and profiles | `config/ingestion/chunking.yaml` | Restart the API and the worker; documents re-chunk on their next ingestion |
| Retrieval size, reranking, the PDF engine, the chunk cap or concurrency for one tenant, project or connection | A runtime setting, over HTTP, with `harborrag settings set` or with `harborrag config import` | Nothing; see [Runtime Settings](runtime-settings.md) |

`harborrag doctor` validates the connector, parser, chunking and model files and names
any blank variable the model catalog or an enabled remote connector needs.
[Checkout quick start](../../developers/checkout-quick-start.md) covers the
`dev.sh` commands that start and rebuild the services.

### Which environment files the CLI loads

Run from a checkout, the CLI loads `env/.env.connector`, `env/.env.parser` and
`env/.env.models`, plus the MinIO credentials and an uncommented
`HARBORRAG_CONTROL_DB_URL` line from `env/.env.database`; a variable you export wins
over every file. [Deployment](../../developers/deployment/README.md) lists the files
each service reads.
