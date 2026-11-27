# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

**Upgrading from 2.0.0a2?** Back up the control database before starting the new API.
Some migrations delete data that a downgrade cannot restore. Start one API process, wait
for `/readyz`, then start the workers, provision MCP reader accounts, and re-ingest each
connection in `force` mode. Check the renamed CLI commands before restarting automation.
Follow the [upgrade checklist](docs/users/upgrading.md#200a2-to-200).

### Added

#### harborrag-app

- Manage projects, connections, ingestions, failed documents, and settings from the CLI using the same names and response formats as the HTTP API. [#133](https://github.com/cbtw-apac/HarborRAG/pull/133)
- Track an ingestion through a resumable event stream and retry failed documents from the failure inbox. [#114](https://github.com/cbtw-apac/HarborRAG/pull/114)
- Inspect and change runtime settings over HTTP or with `harborrag settings`. Changes are stored in PostgreSQL, and each ingestion records which settings revision it used. [#114](https://github.com/cbtw-apac/HarborRAG/pull/114) [#133](https://github.com/cbtw-apac/HarborRAG/pull/133)
- Set file type and size limits, OCR languages, chunk overlap, embedding batch size, and ingestion retry limits without editing worker configuration. [#142](https://github.com/cbtw-apac/HarborRAG/pull/142)

#### harborrag-adapters

- Split oversized Markdown at headings and fences, prose at sentence boundaries, and code at syntax boundaries. Code chunks retain indentation and language information. [#108](https://github.com/cbtw-apac/HarborRAG/pull/108)

#### harborrag-engine

- Filter SharePoint search results by author, timestamps, source URL, and site, drive, or item IDs after re-ingestion. [#121](https://github.com/cbtw-apac/HarborRAG/pull/121)
- Rerank passages before chat answers a question. Configure a rerank model and enable `retrieval.rerank` for the tenant or project. [#115](https://github.com/cbtw-apac/HarborRAG/pull/115)

#### harborrag-mcp-server

- Run `harborrag-mcp --preflight` to check backend connections before opening a client. The HTTP launcher reports missing settings and stops a server that fails its health check. [#137](https://github.com/cbtw-apac/HarborRAG/pull/137)

#### Documentation

- Added [MCP client setup](docs/users/detailed-guides/mcp-server/clients.md) for Claude Desktop, Claude Code, Cursor, VS Code, Codex, Zed, and Gemini CLI, with examples for local stdio and Streamable HTTP.

### Fixed

#### harborrag-adapters

- Confluence pages list each attachment once when the page body and attachment list refer to the same file. [#129](https://github.com/cbtw-apac/HarborRAG/pull/129)
- Local files, GitHub paths, and SharePoint folders with significant spaces retain their names. Files whose names differ only in Unicode normalization remain separate documents. [#138](https://github.com/cbtw-apac/HarborRAG/pull/138) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Local file metadata includes creation and modification times. Reading a file no longer triggers a new version just because its access time changed. [#116](https://github.com/cbtw-apac/HarborRAG/pull/116) [#128](https://github.com/cbtw-apac/HarborRAG/pull/128) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Selecting a parser for one format preserves the other formats in its family. Invalid parser settings now stop API and worker startup and identify what to fix. [#119](https://github.com/cbtw-apac/HarborRAG/pull/119) [#132](https://github.com/cbtw-apac/HarborRAG/pull/132)
- Tables in Word, PowerPoint, HTML, EPUB, and Jira content retain their rows and columns as Markdown. Retrieval tools report the stored chunk kind. [#144](https://github.com/cbtw-apac/HarborRAG/pull/144)

#### harborrag-app

- An unchanged project update preserves its version and ETag and leaves the activity feed unchanged. [#125](https://github.com/cbtw-apac/HarborRAG/pull/125)
- Files excluded by extension appear in ingestion discovery and skip counts. [#136](https://github.com/cbtw-apac/HarborRAG/pull/136)

#### harborrag-engine

- Metadata-only edits update search filters and reuse existing vectors. An unchanged document records the latest source change key so subsequent runs can skip it. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Semantic and graph retrieval work with larger collections. Graph tools can search readable source scopes when a reader has access to more than 10,000 documents. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

#### harborrag-runtime

- A new source run can show `Paused` or `Canceled` in Temporal as well as HarborRAG. Native pause requires `workflow.native_pause`, Temporal Server 1.32.2+, and Python SDK 1.37+. Use the HarborRAG CLI or API to control the run. [#147](https://github.com/cbtw-apac/HarborRAG/pull/147)
- Cancellation during discovery, document processing, or retry preserves run status and counters. Scheduled runs skip a connection that already has an active or paused run. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Document retries now heartbeat. Upgrade workers serving `harborrag-io` before or alongside workers serving `harborrag-transform` so retries can finish. [Worker upgrade order](docs/users/upgrading.md#upgrade-the-services-in-order) · [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Startup reports both schema revisions if a newer build has already migrated the control database. [#109](https://github.com/cbtw-apac/HarborRAG/pull/109)

### Changed

#### harborrag-app

- **Breaking.** Replace `source` and `ingest` commands with `connections` and `ingestions`. Pass `PROJECT_ID/CONNECTION_ID` and use `-o json` for machine-readable output. Update scripts that read the old JSON envelope or exit codes. [CLI migration guide](docs/users/upgrading-2-0-0-automation.md#update-scripts-for-cli-v200) · [#133](https://github.com/cbtw-apac/HarborRAG/pull/133)
- **Breaking.** Chat completions accept `message`, return numbered citations and usage, and create a one-off answer when `session_id` is omitted. Chat sessions and ingestion routes also have new response and event formats. [HTTP client changes](docs/users/upgrading-2-0-0-clients.md) · [#114](https://github.com/cbtw-apac/HarborRAG/pull/114)
- **Breaking.** `auth keys create` and `auth keys revoke --owner` require an explicit tenant. Automation that issues or revokes reader keys must pass `--tenant`. [#133](https://github.com/cbtw-apac/HarborRAG/pull/133)

#### harborrag-engine

- **Breaking.** Replace `updated_at` with `last_modified` in saved search filters and evidence consumers. [Evidence filter changes](docs/users/upgrading-2-0-0-clients.md#evidence-filters) · [#143](https://github.com/cbtw-apac/HarborRAG/pull/143)

#### harborrag-runtime

- Connections are stored in the control database. Fresh installations import the example YAML once. Edit connections through the API or CLI after startup. [#107](https://github.com/cbtw-apac/HarborRAG/pull/107) [#111](https://github.com/cbtw-apac/HarborRAG/pull/111) [#112](https://github.com/cbtw-apac/HarborRAG/pull/112)
- **Breaking.** Configuration files moved into domain directories. Model credentials are set per model family, and the API, workers, and CLI now share `HARBORRAG_TENANT_ID`. [Configuration checklist](docs/users/upgrading-2-0-0-configuration.md) · [#114](https://github.com/cbtw-apac/HarborRAG/pull/114) [#133](https://github.com/cbtw-apac/HarborRAG/pull/133) [#137](https://github.com/cbtw-apac/HarborRAG/pull/137)
- **Breaking.** Ingestion requires the code grammars included with the worker image and the `local` and `production` extras. Custom installations need them before the next run. [#108](https://github.com/cbtw-apac/HarborRAG/pull/108)
- **Breaking.** Update custom integrations for the new Python import paths, service methods, and logger names. [Python and logging changes](docs/users/upgrading-2-0-0-automation.md#python-import-paths-and-service-methods) · [#133](https://github.com/cbtw-apac/HarborRAG/pull/133) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- **Breaking.** Review each connection’s file-type allowlist before its first full sync. Files outside the new defaults can leave the index. [Connection upgrade steps](docs/users/upgrading-2-0-0-operations.md#connections) · [#107](https://github.com/cbtw-apac/HarborRAG/pull/107)
- **Breaking.** SharePoint rejects a page size above 100. Set older connections that use 200 to 100 or less before their next sync. [#135](https://github.com/cbtw-apac/HarborRAG/pull/135)

#### Deployment

- The API migrates legacy source rows into connections and removes retired agent data. Check database lock requirements and take a backup before migration. [Migration table](docs/users/upgrading-2-0-0-operations.md#control-database-migrations) · [#111](https://github.com/cbtw-apac/HarborRAG/pull/111) [#130](https://github.com/cbtw-apac/HarborRAG/pull/130) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- The documentation dispatch workflow can notify a downstream documentation repository on a main-branch documentation change or a HarborRAG release. [#127](https://github.com/cbtw-apac/HarborRAG/pull/127)
- MCP containers join the local data network and publish HTTP on loopback. Docker Desktop users can run them without enabling host networking. [#137](https://github.com/cbtw-apac/HarborRAG/pull/137)
- **Breaking.** If Qdrant uses a nondefault HTTP port and gRPC is preferred, set `HARBORRAG_QDRANT_GRPC_PORT` explicitly. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

#### Documentation

- The [Quick Start](docs/getting-started/quick-start.md) walks from a local checkout to a cited answer. The [MCP guide](docs/users/detailed-guides/mcp-server/setup-and-integration.md) and [upgrade guide](docs/users/upgrading.md) now link to separate client, configuration, and operations instructions.
- Clarified model and connector setup, Docker Desktop use, and MCP search filters. [#110](https://github.com/cbtw-apac/HarborRAG/pull/110) [#118](https://github.com/cbtw-apac/HarborRAG/pull/118)

### Removed

#### harborrag-app

- **Breaking.** `harborrag init`, `harborrag retrieve`, and `harborrag topology` are retired. Run new installations from a checkout, and use MCP search or `harborrag chat` for retrieval. Existing projects with a `harborrag.yaml` still work. [#133](https://github.com/cbtw-apac/HarborRAG/pull/133)

#### harborrag-runtime

- **Breaking.** Legacy SDK knowledge methods, unused storage and parser aliases, and the unused memory extraction queue have been removed. [Removed symbols and replacements](docs/users/upgrading-2-0-0-automation.md#removed-symbols-and-their-replacements) · [#133](https://github.com/cbtw-apac/HarborRAG/pull/133) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

### Security

#### harborrag-adapters

- **Breaking.** Connectors constructed in Python require explicit credentials. GitHub and SharePoint enforce the configured source root, and outbound requests validate the destination before following redirects. [#117](https://github.com/cbtw-apac/HarborRAG/pull/117) [#120](https://github.com/cbtw-apac/HarborRAG/pull/120) [#124](https://github.com/cbtw-apac/HarborRAG/pull/124) [#139](https://github.com/cbtw-apac/HarborRAG/pull/139)
- **Breaking.** For model endpoints that require authentication, set an explicit API key on the deployment. A keyless configuration no longer forwards a provider key from the process environment. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- SharePoint sign-in errors redact client secrets and tenant details. Model validation, parser failures, and database diagnostics also omit sensitive inputs. [#123](https://github.com/cbtw-apac/HarborRAG/pull/123) [#143](https://github.com/cbtw-apac/HarborRAG/pull/143) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Jira role- or group-restricted comments are excluded from ingestion. Run each Jira connection once without a date filter to remove previously indexed restricted comments from active search. [Jira refresh instructions](docs/users/upgrading-2-0-0-operations.md#run-each-jira-connection-once-in-full) · [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

#### harborrag-app

- **Breaking.** HTTP API keys read with their owner’s permissions. Chat sessions and idempotent replays also use the owner identity. Under `source_acl`, connection listings and MCP source counts reflect the documents the reader can access. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- Scraping the operations metrics endpoint requires a nonblank `HARBORRAG_METRICS_SCRAPE_TOKEN`. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

#### harborrag-memory

- Erasing a user also erases their chat sessions, messages, session memories, and stored completion requests. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

#### Deployment

- **Breaking.** Migration `0049` clears legacy plaintext credentials and marks affected connections as errors. Re-enter the fields named in each connection’s status before running it. [Recovery steps](docs/users/upgrading-2-0-0-operations.md#set-cleared-credentials-again) · [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- **Breaking.** MCP and Explorer use read-only PostgreSQL and MinIO accounts. On Compose, run `scripts/deployment/dev.sh mcp-access` after upgrading and after recreating data volumes. MCP HTTP also limits requests per credential and checks the host and origin. [#137](https://github.com/cbtw-apac/HarborRAG/pull/137) [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)
- **Breaking.** Remote Qdrant, FalkorDB, and Temporal require TLS in staging and production. Insecure-remote overrides apply only in development. [#140](https://github.com/cbtw-apac/HarborRAG/pull/140)

### Known limitations

Graph and vector indexing still share a failure path. Permanently rejected documents may be fetched
again on the next run. Large graph searches, bulk retries, and replay of some closed alpha1
workflows also have restrictions. [Known limitations and affected operations](docs/users/upgrading-2-0-0-operations.md#known-limitations)

## [2.0.0a2] - 2026-10-26

Stored data is not migrated from 2.0.0a1: back up the control database and re-ingest every
source. Follow the [upgrade guide](docs/users/upgrading.md) before upgrading; entries marked
**Breaking** need action.

### Added

#### harborrag

- The `harborrag` CLI ships with every install; `harborrag[local]` adds the local-stack clients [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)

#### harborrag-adapters

- Parsing for legacy Word `.doc` and PowerPoint `.ppt`/`.pps` files [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- SharePoint connector: crawl sites, libraries, folders or selected files, with allowlists and optional site pages [#90](https://github.com/cbtw-apac/HarborRAG/pull/90)
- SharePoint device-code sign-in, including for the Docker worker [#90](https://github.com/cbtw-apac/HarborRAG/pull/90) [#99](https://github.com/cbtw-apac/HarborRAG/pull/99)
- Jira custom fields are filterable on issues and their attachments [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

#### harborrag-app

- `harborrag init` scaffolds a ready-to-run project with provider presets and data-source setup [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)
- `harborrag ingest run` runs an ingestion in-process, without Temporal [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)
- `harborrag doctor` checks the project, configuration and every backing store [#59](https://github.com/cbtw-apac/HarborRAG/pull/59) [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- `harborrag auth keys` issues, lists and revokes tenant-bound MCP reader keys [#85](https://github.com/cbtw-apac/HarborRAG/pull/85) [#89](https://github.com/cbtw-apac/HarborRAG/pull/89)
- `--timeout` option for `ingest watch`, `wait` and `start --wait` [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- `ingest start` takes the connector positionally, like `ingest run` [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Pause, resume and cancel actions are recorded in the run's events [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

#### harborrag-engine

- Placeholder graph nodes for links to sources not yet ingested, replaced once the target is published [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

#### harborrag-mcp-server

- `find_entities` tool to search whole entities by facet [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

#### harborrag-runtime

- Automatic purge of retired document versions after a configurable retention period [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Searchable entity summaries that lead to their supporting evidence [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Summary facets: filterable attributes copied from connector fields [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Configurable entity summary length [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

### Fixed

#### harborrag-adapters

- Corrupt or encrypted `.xls` files are reported as parser rejections [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- SharePoint handles deleted items and expired tokens without failing the run [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Legacy secrets are migrated to the correct tenant [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-app

- Interrupting or failing `ingest run` no longer leaves the run stuck [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- The API no longer submits the same run twice [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- `ingest pause`, `resume`, `cancel`, `status` and `watch` report correct results for finished and partial runs [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Clearer error messages that never include credentials, and faster CLI startup [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)

#### harborrag-engine

- Retrieval works for users who can read more than 10,000 documents [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Entity search honours all filters, and facet filters are case-insensitive [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Graph reads and relation repair handle placeholder nodes and cross-scope links reliably [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-mcp-server

- Tool output schemas are valid for MCP clients again [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- `find_entities` reports unknown facets instead of returning an empty result [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- A broken API key file denies access instead of revoking every key [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Over-limit `mcp.yaml` values are lowered with a warning instead of stopping the server [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-memory

- Memory retention settings are enforced [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Memory recall works without an embedding model [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-runtime

- Large sources no longer time out during discovery [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Pausing and resuming an ingestion no longer fails the run or loses its progress [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Re-ingesting content while its old version is being purged retries instead of failing [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- A retried chat request no longer interferes with a slow response still in progress [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Summary and chat client failures no longer take down ingestion workers [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- A late cancellation can no longer overwrite a completed agent run [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- A warning is logged when a removed storage prefix setting is still set [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- One concise log line per failed document [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)

#### Deployment

- Removed unused variables from `env-example` [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

### Changed

#### harborrag-adapters

- **Breaking:** one storage namespace per tenant across Qdrant, FalkorDB and the object store; re-ingest after upgrading [#85](https://github.com/cbtw-apac/HarborRAG/pull/85) [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- LiteParse is the default parser, and the OCR server is now opt-in [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- `config/parsers.yaml` supports environment variables [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- **Breaking:** SharePoint requires the `Sites.Selected` permission and limits file types by default [#90](https://github.com/cbtw-apac/HarborRAG/pull/90)
- SharePoint shares one rate limit across workers, skips unreadable folders, and caps retries per file [#96](https://github.com/cbtw-apac/HarborRAG/pull/96) [#101](https://github.com/cbtw-apac/HarborRAG/pull/101)
- **Breaking:** Jira custom-field filter keys handle accented names; update saved filters and re-ingest [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Local connectors reject unknown filters, and partial scans no longer retire documents [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)

#### harborrag-app

- **Breaking:** standardized CLI exit codes; failed or partial runs now exit 1 [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- In a repository checkout, ingestion uses the stack's PostgreSQL and object-store settings instead of a local SQLite database [#95](https://github.com/cbtw-apac/HarborRAG/pull/95) [#99](https://github.com/cbtw-apac/HarborRAG/pull/99)
- `auth keys` requires an up-to-date control database and reports clearer errors [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Run ids cannot be reused [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Clearer ingestion API responses when Temporal is unreachable or no longer knows a run [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Inline progress display replaces the full-screen dashboard [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)
- Local commands no longer require Temporal to be installed [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)
- `harborrag init` keeps existing project files [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- CLI file paths resolve from the current directory [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-engine

- Each source link is stored as a single graph edge, and duplicate attachment edges are removed [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Duplicate search results are collapsed per source instead of across the whole corpus [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Vector search results link directly to their graph nodes [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- More accurate unresolved-relation counts [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

#### harborrag-mcp-server

- **Breaking:** graph chunk edges now point from structure to chunk, and graph traversal limits are lower [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- The MCP Explorer respects each tenant's limits [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-memory

- Project-scope memories stay within the session unless enabled, and summary prompts are bounded [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-runtime

- Summarization is off by default [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Summaries indicate when some source documents are still missing [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Summary reads no longer lock or write data [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Faster summary updates and publication [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Retired-version cleanup starts sooner and exposes metrics [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- Ingestion defaults tuned for smaller hosts [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Faster ingestion workflows, with per-document cost independent of source size [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- The secrets encryption key is only required by processes that open secrets [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- Failed documents report a clear error code and stage, and parser rejections are not retried [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Startup fails on mismatched embedding dimensions [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)

#### Deployment

- MinIO uses a new image that runs as a non-root user [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

### Deprecated

#### harborrag-adapters

- The Confluence `max_child_pages` setting is ignored with a warning [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

### Removed

#### harborrag-app

- Textual ingestion dashboard [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)

#### harborrag-mcp-server

- **Breaking:** `composed_evidence_search`, `verify_citations` and `get_document_metadata` tools; use `vector_search`, `fetch_evidence` and `get_document_context` instead [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)

#### harborrag-memory

- Unused `HARBORRAG_MEMORY_PII_REDACTION` setting [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

### Security

#### harborrag-adapters

- The `.ppt` parser is protected against crafted files that expand into huge output [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- SharePoint tokens are stored safely, and pre-authenticated download links are no longer kept [#96](https://github.com/cbtw-apac/HarborRAG/pull/96) [#98](https://github.com/cbtw-apac/HarborRAG/pull/98)
- Model configuration errors no longer print API keys [#98](https://github.com/cbtw-apac/HarborRAG/pull/98)

#### harborrag-app

- Project discovery only trusts directories owned by the current user [#59](https://github.com/cbtw-apac/HarborRAG/pull/59)
- Errors and logs no longer expose credentials [#95](https://github.com/cbtw-apac/HarborRAG/pull/95)
- Unauthenticated mode refuses non-local requests unless explicitly allowed [#98](https://github.com/cbtw-apac/HarborRAG/pull/98)

#### harborrag-core

- More credential field names are detected and redacted [#98](https://github.com/cbtw-apac/HarborRAG/pull/98)

#### harborrag-engine

- Retrieval diagnostics no longer reveal documents the caller cannot read [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-mcp-server

- Reader keys cannot target all tenants [#85](https://github.com/cbtw-apac/HarborRAG/pull/85)
- In API key mode, the owner token is only accepted for single-tenant deployments [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)
- The MCP server no longer receives the secrets encryption key [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-memory

- Shared memories can only be deleted or replaced by their author or a user with the required role [#96](https://github.com/cbtw-apac/HarborRAG/pull/96)

#### harborrag-runtime

- A tenant reindex no longer touches other tenants' cleanup jobs [#98](https://github.com/cbtw-apac/HarborRAG/pull/98)

## [2.0.0a1] - 2026-08-27

This is the first public HarborRAG 2.0 alpha release. Python package metadata,
distribution filenames, dependency pins, and release tags use the canonical
PEP 440 version `2.0.0a1`; release tooling also accepts `2.0.0-alpha` as an
operator-facing alias.

### Added

#### harborrag

- HarborRAG 2.0: a modular, provider-agnostic RAG framework built from eight independently testable packages [#1](https://github.com/cbtw-apac/HarborRAG/pull/1) [#6](https://github.com/cbtw-apac/HarborRAG/pull/6)
- One `harborrag` install, with extras for the CLI, HTTP API, MCP server, chat, Temporal and each storage provider [#31](https://github.com/cbtw-apac/HarborRAG/pull/31)
- Python SDK with ingestion, retrieval, graph and chat services behind one async client [#32](https://github.com/cbtw-apac/HarborRAG/pull/32)

#### harborrag-adapters

- Connectors for local files, GitHub, Confluence, Jira and SharePoint, including comments and attachments [#2](https://github.com/cbtw-apac/HarborRAG/pull/2) [#6](https://github.com/cbtw-apac/HarborRAG/pull/6)
- Parsers for text, Markdown, HTML, JSON, CSV/Excel, Word, ODT, EPUB, PowerPoint, images and PDF [#2](https://github.com/cbtw-apac/HarborRAG/pull/2) [#9](https://github.com/cbtw-apac/HarborRAG/pull/9)
- Choice of PDF engines: PyMuPDF, Docling, LiteParse, MinerU or PaddleOCR [#2](https://github.com/cbtw-apac/HarborRAG/pull/2) [#9](https://github.com/cbtw-apac/HarborRAG/pull/9)
- Chat, embedding and reranking clients with configurable providers [#4](https://github.com/cbtw-apac/HarborRAG/pull/4)
- Storage on PostgreSQL, S3-compatible object storage, Qdrant, FalkorDB and Redis [#5](https://github.com/cbtw-apac/HarborRAG/pull/5) [#7](https://github.com/cbtw-apac/HarborRAG/pull/7)
- Confluence pages are converted into structured text and tables [#16](https://github.com/cbtw-apac/HarborRAG/pull/16)
- Optional attachment ingestion for Confluence and Jira [#19](https://github.com/cbtw-apac/HarborRAG/pull/19)
- Local file discovery reports skipped files with a reason [#14](https://github.com/cbtw-apac/HarborRAG/pull/14)

#### harborrag-app

- `harborrag` operator CLI [#1](https://github.com/cbtw-apac/HarborRAG/pull/1)
- HTTP control-plane API with authentication, role-based access, health checks and a standard error format [#7](https://github.com/cbtw-apac/HarborRAG/pull/7)
- Read APIs for projects, sources, activity, settings and metrics [#11](https://github.com/cbtw-apac/HarborRAG/pull/11)
- Ingestion and connection APIs with live progress streaming [#25](https://github.com/cbtw-apac/HarborRAG/pull/25) [#34](https://github.com/cbtw-apac/HarborRAG/pull/34)

#### harborrag-engine

- Ingestion pipeline that skips unchanged documents instead of re-parsing and re-embedding them [#12](https://github.com/cbtw-apac/HarborRAG/pull/12) [#22](https://github.com/cbtw-apac/HarborRAG/pull/22)
- Table-aware chunking [#15](https://github.com/cbtw-apac/HarborRAG/pull/15) [#16](https://github.com/cbtw-apac/HarborRAG/pull/16)
- Dense, sparse and hybrid retrieval that only cites the current version of a document [#12](https://github.com/cbtw-apac/HarborRAG/pull/12) [#27](https://github.com/cbtw-apac/HarborRAG/pull/27)
- Knowledge graph of document structure and source relationships [#12](https://github.com/cbtw-apac/HarborRAG/pull/12) [#38](https://github.com/cbtw-apac/HarborRAG/pull/38)
- Retrieval-grounded chat with streaming, and a bounded multi-turn agent [#22](https://github.com/cbtw-apac/HarborRAG/pull/22)

#### harborrag-mcp-server

- MCP server with `vector_search` and graph tools for triplets, paths and subgraphs [#10](https://github.com/cbtw-apac/HarborRAG/pull/10) [#22](https://github.com/cbtw-apac/HarborRAG/pull/22) [#27](https://github.com/cbtw-apac/HarborRAG/pull/27)

#### harborrag-memory

- Short-term and working memory for chat sessions [#22](https://github.com/cbtw-apac/HarborRAG/pull/22)

#### harborrag-runtime

- Durable ingestion on Temporal: survives worker restarts, retries single documents, and supports pause, resume and cancel [#12](https://github.com/cbtw-apac/HarborRAG/pull/12) [#24](https://github.com/cbtw-apac/HarborRAG/pull/24)
- Rebuild vector and graph indexes from stored content without re-crawling sources [#12](https://github.com/cbtw-apac/HarborRAG/pull/12)
- Temporal settings in YAML configuration [#32](https://github.com/cbtw-apac/HarborRAG/pull/32)

#### Deployment

- Docker Compose stack and `dev.sh` scripts to run the full stack locally [#22](https://github.com/cbtw-apac/HarborRAG/pull/22) [#32](https://github.com/cbtw-apac/HarborRAG/pull/32)

#### Documentation

- Public documentation website with interactive architecture and feature sections [#31](https://github.com/cbtw-apac/HarborRAG/pull/31) [#40](https://github.com/cbtw-apac/HarborRAG/pull/40)
- Data-lifecycle, runtime-reliability, publication and security guides [#31](https://github.com/cbtw-apac/HarborRAG/pull/31)

#### Development

- Pre-commit and pre-push hooks running the same quality gates as CI [#28](https://github.com/cbtw-apac/HarborRAG/pull/28)

### Fixed

#### harborrag-adapters

- CMYK images are converted before OCR [#37](https://github.com/cbtw-apac/HarborRAG/pull/37)
- Clearer errors for undecodable text files and password-protected presentations [#18](https://github.com/cbtw-apac/HarborRAG/pull/18) [#22](https://github.com/cbtw-apac/HarborRAG/pull/22)

#### harborrag-engine

- Retrieval never returns a superseded document version during a concurrent publish [#31](https://github.com/cbtw-apac/HarborRAG/pull/31)
- Graph subgraph search returns complete results and no longer times out on large graphs [#35](https://github.com/cbtw-apac/HarborRAG/pull/35)
- Jira subtasks no longer appear as a separate node from their parent issue [#33](https://github.com/cbtw-apac/HarborRAG/pull/33)
- The graph keeps page and issue membership separate from document structure across all connectors [#38](https://github.com/cbtw-apac/HarborRAG/pull/38)
- Relation filters only accept relation types the graph actually stores [#36](https://github.com/cbtw-apac/HarborRAG/pull/36)

#### harborrag-mcp-server

- The local MCP HTTP server works on Windows [#26](https://github.com/cbtw-apac/HarborRAG/pull/26)

#### harborrag-runtime

- Parsing falls back to in-process when a subprocess cannot start, instead of failing the document [#30](https://github.com/cbtw-apac/HarborRAG/pull/30)

#### Documentation

- Website build fixes for link checking, coverage pages, UTF-8 output and asset paths [#43](https://github.com/cbtw-apac/HarborRAG/pull/43) [#44](https://github.com/cbtw-apac/HarborRAG/pull/44) [#45](https://github.com/cbtw-apac/HarborRAG/pull/45) [#48](https://github.com/cbtw-apac/HarborRAG/pull/48)

### Changed

#### harborrag

- License changed to Apache 2.0 [#3](https://github.com/cbtw-apac/HarborRAG/pull/3)

### Security

#### harborrag-adapters

- Connector credentials are encrypted at rest [#25](https://github.com/cbtw-apac/HarborRAG/pull/25)
- Outbound requests to private or disallowed addresses are blocked [#8](https://github.com/cbtw-apac/HarborRAG/pull/8)

#### harborrag-app

- Every control-plane API call is limited to the caller's tenant [#23](https://github.com/cbtw-apac/HarborRAG/pull/23)
- Secrets are redacted from API responses and diagnostics [#8](https://github.com/cbtw-apac/HarborRAG/pull/8) [#11](https://github.com/cbtw-apac/HarborRAG/pull/11)
- Production refuses to run on the default SQLite database [#8](https://github.com/cbtw-apac/HarborRAG/pull/8)

#### harborrag-mcp-server

- The audit log refuses symlinks and other unsafe files [#26](https://github.com/cbtw-apac/HarborRAG/pull/26) [#27](https://github.com/cbtw-apac/HarborRAG/pull/27)

#### Deployment

- Database, MinIO and Grafana passwords must be supplied by the operator [#31](https://github.com/cbtw-apac/HarborRAG/pull/31)

[Unreleased]: https://github.com/cbtw-apac/HarborRAG/compare/harborrag-v2.0.0a2...HEAD
[2.0.0a2]: https://github.com/cbtw-apac/HarborRAG/compare/harborrag-v2.0.0a1...harborrag-v2.0.0a2
[2.0.0a1]: https://github.com/cbtw-apac/HarborRAG/releases/tag/harborrag-v2.0.0a1
