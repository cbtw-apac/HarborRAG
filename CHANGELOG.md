# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0a2] - 2026-10-26

### Upgrading from 2.0.0a1

Stored data is not migrated, so plan a reset and a full re-ingest.

1. Stop schedules and let running ingestions finish, or cancel them.
2. Back up the PostgreSQL control database; restoring it is the only rollback.
3. Replace the old Qdrant and FalkorDB prefix settings with `HARBORRAG_STORAGE_NAMESPACE_PREFIX`.
4. Clear the alpha1 Qdrant collections, FalkorDB graphs and object-store data.
5. Run the database migrations once, from a single process, before starting the services.
6. Re-ingest every source, and update saved Jira filters that use accented field names.
7. Summaries are now off by default; to keep them, enable them in `config/topology/graph_build.yaml` and run `harborrag topology summaries backfill`.
8. SharePoint: grant the app only the `Sites.Selected` permission.

### Added

#### harborrag

- The `harborrag` CLI ships with every install; `harborrag[local]` adds the local-stack clients [#59]

#### harborrag-adapters

- Parsing for legacy Word `.doc` and PowerPoint `.ppt`/`.pps` files [#85]
- SharePoint connector: crawl sites, libraries, folders or selected files, with allowlists and optional site pages [#90]
- SharePoint device-code sign-in, including for the Docker worker [#90] [#99]
- Jira custom fields are filterable on issues and their attachments [#85]

#### harborrag-app

- `harborrag init` scaffolds a ready-to-run project with provider presets and data-source setup [#59]
- `harborrag ingest run` runs an ingestion in-process, without Temporal [#59]
- `harborrag doctor` checks the project, configuration and every backing store [#59] [#95]
- `harborrag auth keys` issues, lists and revokes tenant-bound MCP reader keys [#85] [#89]
- `--timeout` option for `ingest watch`, `wait` and `start --wait` [#95]
- `ingest start` takes the connector positionally, like `ingest run` [#95]
- Pause, resume and cancel actions are recorded in the run's events [#85]

#### harborrag-engine

- Placeholder graph nodes for links to sources not yet ingested, replaced once the target is published [#85]

#### harborrag-mcp-server

- `find_entities` tool to search whole entities by facet [#85]

#### harborrag-runtime

- Automatic purge of retired document versions after a configurable retention period [#85]
- Searchable entity summaries that lead to their supporting evidence [#85]
- Summary facets: filterable attributes copied from connector fields [#85]
- Configurable entity summary length [#85]

### Fixed

#### harborrag-adapters

- Corrupt or encrypted `.xls` files are reported as parser rejections [#85]
- SharePoint handles deleted items and expired tokens without failing the run [#96]
- Legacy secrets are migrated to the correct tenant [#96]

#### harborrag-app

- Interrupting or failing `ingest run` no longer leaves the run stuck [#95]
- The API no longer submits the same run twice [#95]
- `ingest pause`, `resume`, `cancel`, `status` and `watch` report correct results for finished and partial runs [#95]
- Clearer error messages that never include credentials, and faster CLI startup [#95]

#### harborrag-engine

- Retrieval works for users who can read more than 10,000 documents [#96]
- Entity search honours all filters, and facet filters are case-insensitive [#96]
- Graph reads and relation repair handle placeholder nodes and cross-scope links reliably [#96]

#### harborrag-mcp-server

- Tool output schemas are valid for MCP clients again [#85]
- `find_entities` reports unknown facets instead of returning an empty result [#96]
- A broken API key file denies access instead of revoking every key [#96]
- Over-limit `mcp.yaml` values are lowered with a warning instead of stopping the server [#96]

#### harborrag-memory

- Memory retention settings are enforced [#96]
- Memory recall works without an embedding model [#96]

#### harborrag-runtime

- Large sources no longer time out during discovery [#85]
- Pausing and resuming an ingestion no longer fails the run or loses its progress [#96]
- Re-ingesting content while its old version is being purged retries instead of failing [#96]
- A retried chat request no longer interferes with a slow response still in progress [#96]
- Summary and chat client failures no longer take down ingestion workers [#96]
- A late cancellation can no longer overwrite a completed agent run [#96]
- A warning is logged when a removed storage prefix setting is still set [#96]
- One concise log line per failed document [#95]

#### Deployment

- Removed unused variables from `env-example` [#96]

### Changed

#### harborrag-adapters

- Breaking: one storage namespace per tenant across Qdrant, FalkorDB and the object store; re-ingest after upgrading [#85] [#96]
- LiteParse is the default parser, and the OCR server is now opt-in [#85]
- `config/parsers.yaml` supports environment variables [#85]
- Breaking: SharePoint requires the `Sites.Selected` permission and limits file types by default [#90]
- SharePoint shares one rate limit across workers, skips unreadable folders, and caps retries per file [#96] [#101]
- Breaking: Jira custom-field filter keys handle accented names; update saved filters and re-ingest [#96]
- Local connectors reject unknown filters, and partial scans no longer retire documents [#95]

#### harborrag-app

- Breaking: standardized CLI exit codes; failed or partial runs now exit 1 [#95]
- In a repository checkout, ingestion uses the stack's PostgreSQL and object-store settings instead of a local SQLite database [#95] [#99]
- `auth keys` requires an up-to-date control database and reports clearer errors [#95]
- Run ids cannot be reused [#95]
- Clearer ingestion API responses when Temporal is unreachable or no longer knows a run [#95]
- Inline progress display replaces the full-screen dashboard [#59]
- Local commands no longer require Temporal to be installed [#59]
- `harborrag init` keeps existing project files [#96]
- CLI file paths resolve from the current directory [#96]

#### harborrag-engine

- Each source link is stored as a single graph edge, and duplicate attachment edges are removed [#85]
- Duplicate search results are collapsed per source instead of across the whole corpus [#85]
- Vector search results link directly to their graph nodes [#85]
- More accurate unresolved-relation counts [#85]

#### harborrag-mcp-server

- Breaking: graph chunk edges now point from structure to chunk, and graph traversal limits are lower [#96]
- The MCP Explorer respects each tenant's limits [#96]

#### harborrag-memory

- Project-scope memories stay within the session unless enabled, and summary prompts are bounded [#96]

#### harborrag-runtime

- Summarization is off by default [#85]
- Summaries indicate when some source documents are still missing [#85]
- Summary reads no longer lock or write data [#85]
- Faster summary updates and publication [#96]
- Retired-version cleanup starts sooner and exposes metrics [#96]
- Ingestion defaults tuned for smaller hosts [#85]
- Faster ingestion workflows, with per-document cost independent of source size [#85]
- The secrets encryption key is only required by processes that open secrets [#85]
- Failed documents report a clear error code and stage, and parser rejections are not retried [#95]
- Startup fails on mismatched embedding dimensions [#95]

#### Deployment

- MinIO uses a new image that runs as a non-root user [#85]

### Deprecated

#### harborrag-adapters

- The Confluence `max_child_pages` setting is ignored with a warning [#96]

### Removed

#### harborrag-app

- Textual ingestion dashboard [#59]

#### harborrag-mcp-server

- Breaking: `composed_evidence_search`, `verify_citations` and `get_document_metadata` tools; use `vector_search`, `fetch_evidence` and `get_document_context` instead [#85]

#### harborrag-memory

- Unused `HARBORRAG_MEMORY_PII_REDACTION` setting [#96]

### Security

#### harborrag-adapters

- The `.ppt` parser is protected against crafted files that expand into huge output [#96]
- SharePoint tokens are stored safely, and pre-authenticated download links are no longer kept [#96] [#98]
- Model configuration errors no longer print API keys [#98]

#### harborrag-app

- Project discovery only trusts directories owned by the current user [#59]
- Errors and logs no longer expose credentials [#95]
- Unauthenticated mode refuses non-local requests unless explicitly allowed [#98]

#### harborrag-core

- More credential field names are detected and redacted [#98]

#### harborrag-engine

- Retrieval diagnostics no longer reveal documents the caller cannot read [#96]

#### harborrag-mcp-server

- Reader keys cannot target all tenants [#85]
- In API key mode, the owner token is only accepted for single-tenant deployments [#96]
- The MCP server no longer receives the secrets encryption key [#96]

#### harborrag-memory

- Shared memories can only be deleted or replaced by their author or a user with the required role [#96]

#### harborrag-runtime

- A tenant reindex no longer touches other tenants' cleanup jobs [#98]

## [2.0.0a1] - 2026-08-27

This is the first public HarborRAG 2.0 alpha release. Python package metadata,
distribution filenames, dependency pins, and release tags use the canonical
PEP 440 version `2.0.0a1`; release tooling also accepts `2.0.0-alpha` as an
operator-facing alias.

### Added

#### harborrag

- HarborRAG 2.0: a modular, provider-agnostic RAG framework built from eight independently testable packages [#1] [#6]
- One `harborrag` install, with extras for the CLI, HTTP API, MCP server, chat, Temporal and each storage provider [#31]
- Python SDK with ingestion, retrieval, graph and chat services behind one async client [#32]

#### harborrag-adapters

- Connectors for local files, GitHub, Confluence, Jira and SharePoint, including comments and attachments [#2] [#6]
- Parsers for text, Markdown, HTML, JSON, CSV/Excel, Word, ODT, EPUB, PowerPoint, images and PDF [#2] [#9]
- Choice of PDF engines: PyMuPDF, Docling, LiteParse, MinerU or PaddleOCR [#2] [#9]
- Chat, embedding and reranking clients with configurable providers [#4]
- Storage on PostgreSQL, S3-compatible object storage, Qdrant, FalkorDB and Redis [#5] [#7]
- Confluence pages are converted into structured text and tables [#16]
- Optional attachment ingestion for Confluence and Jira [#19]
- Local file discovery reports skipped files with a reason [#14]

#### harborrag-app

- `harborrag` operator CLI [#1]
- HTTP control-plane API with authentication, role-based access, health checks and a standard error format [#7]
- Read APIs for projects, sources, activity, settings and metrics [#11]
- Ingestion and connection APIs with live progress streaming [#25] [#34]

#### harborrag-engine

- Ingestion pipeline that skips unchanged documents instead of re-parsing and re-embedding them [#12] [#22]
- Table-aware chunking [#15] [#16]
- Dense, sparse and hybrid retrieval that only cites the current version of a document [#12] [#27]
- Knowledge graph of document structure and source relationships [#12] [#38]
- Retrieval-grounded chat with streaming, and a bounded multi-turn agent [#22]

#### harborrag-mcp-server

- MCP server with `vector_search` and graph tools for triplets, paths and subgraphs [#10] [#22] [#27]

#### harborrag-memory

- Short-term and working memory for chat sessions [#22]

#### harborrag-runtime

- Durable ingestion on Temporal: survives worker restarts, retries single documents, and supports pause, resume and cancel [#12] [#24]
- Rebuild vector and graph indexes from stored content without re-crawling sources [#12]
- Temporal settings in YAML configuration [#32]

#### Deployment

- Docker Compose stack and `dev.sh` scripts to run the full stack locally [#22] [#32]

#### Documentation

- Public documentation website with interactive architecture and feature sections [#31] [#40]
- Data-lifecycle, runtime-reliability, publication and security guides [#31]

#### Development

- Pre-commit and pre-push hooks running the same quality gates as CI [#28]

### Fixed

#### harborrag-adapters

- CMYK images are converted before OCR [#37]
- Clearer errors for undecodable text files and password-protected presentations [#18] [#22]

#### harborrag-engine

- Retrieval never returns a superseded document version during a concurrent publish [#31]
- Graph subgraph search returns complete results and no longer times out on large graphs [#35]
- Jira subtasks no longer appear as a separate node from their parent issue [#33]
- The graph keeps page and issue membership separate from document structure across all connectors [#38]
- Relation filters only accept relation types the graph actually stores [#36]

#### harborrag-mcp-server

- The local MCP HTTP server works on Windows [#26]

#### harborrag-runtime

- Parsing falls back to in-process when a subprocess cannot start, instead of failing the document [#30]

#### Documentation

- Website build fixes for link checking, coverage pages, UTF-8 output and asset paths [#43] [#44] [#45] [#48]

### Changed

#### harborrag

- License changed to Apache 2.0 [#3]

### Security

#### harborrag-adapters

- Connector credentials are encrypted at rest [#25]
- Outbound requests to private or disallowed addresses are blocked [#8]

#### harborrag-app

- Every control-plane API call is limited to the caller's tenant [#23]
- Secrets are redacted from API responses and diagnostics [#8] [#11]
- Production refuses to run on the default SQLite database [#8]

#### harborrag-mcp-server

- The audit log refuses symlinks and other unsafe files [#26] [#27]

#### Deployment

- Database, MinIO and Grafana passwords must be supplied by the operator [#31]

[2.0.0a2]: https://github.com/cbtw-apac/HarborRAG/compare/harborrag-v2.0.0a1...harborrag-v2.0.0a2
[2.0.0a1]: https://github.com/cbtw-apac/HarborRAG/releases/tag/harborrag-v2.0.0a1
