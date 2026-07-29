# harborrag-adapters

Provider adapters for HarborRAG.

This package owns the code that talks to external systems and extracts text from
raw source files. It sits between:

- `harborrag-core`, which owns shared domain schemas such as `SourceRecord`,
  `RawDocument`, `ParseInput`, and `ParsedDocument`.
- `harborrag-runtime`, which should own orchestration, concurrency, scheduling,
  backpressure, retries across tasks, and large ingestion pipelines.

The adapter layer should stay focused on source-specific behavior: auth, API
pagination, request retry hints, payload safety limits, parser routing, and clear
errors for missing optional dependencies.

## Public extension API

Each adapter family owns its registry and factory at the variation point:
connectors, parsers, model providers, and repository backends are registered
independently. There is intentionally no cross-family registry because adding
one provider must not require editing unrelated adapter families.

## File ownership and test doubles

- `connectors/base.py` and `parsers/base.py` define contracts. Production
  implementations belong in provider or format modules under those packages.
- Implemented connectors and parsers do not keep production `mock.py` modules;
  their fakes and test doubles belong under `tests/`.
- Repository families expose provider-neutral `Harbor*` contracts plus real
  provider packages. Their deterministic fakes belong under `tests/`; production
  repository packages do not ship mock backends.
- Provider construction belongs to the registry or factory in that capability;
  domain schemas remain in `harborrag-core`, and workflow orchestration remains
  outside adapters.

## Contributor and teammate deliverables

A new adapter contribution should include its implementation and validated
config, public exports or registry registration, focused unit/failure/security
tests, optional-dependency declarations, and documentation or example env keys.
Never commit credentials or use real provider payloads as fixtures.

## Install

Base connector support only requires `requests` plus `harborrag-core`:

```bash
pip install -e packages/harborrag-core -e packages/harborrag-adapters
```

Install common parser dependencies when parsing office files, images, HTML, and
PDFs through the default parser stack:

```bash
pip install -e "packages/harborrag-adapters[parsers]"
```

Install the optional third-party chunking providers used for oversized text
refinement and Markdown, HTML, or JSON structure recovery:

```bash
pip install -e "packages/harborrag-adapters[chunking]"
```

The provider implementations live directly under `chunking/` in capability-
named modules: `recursive.py`, `markdownsplitter.py`, `htmlsplitter.py`, and
`jsonsplitter.py`.
Use `HarborChunk` to construct one by its registered name (`recursive`,
`markdown`, `html`, or `json`). Every implementation derives from
`HarborBaseChunk` and returns HarborRAG-owned `TextSplit` values; engine policy
never depends on framework-owned types. `HarborChunk.available(name)` checks an
optional provider without importing it, allowing ingestion composition to use
normalized parser elements when a structural splitter is not installed.

Install advanced PDF backends separately when needed. This extra also includes
RapidOCR with its default ONNX Runtime CPU engine:

```bash
pip install -e "packages/harborrag-adapters[pdf]"
```

For a Docling/RapidOCR-only deployment, use the narrower extra:

```bash
pip install -e "packages/harborrag-adapters[pdf-docling]"
```

SQLite database, state, filesystem, and memory repositories are included in the
base install. Install only the extras needed by deployed services:

```bash
pip install -e "packages/harborrag-adapters[redis,qdrant,falkordb,postgres,s3]"
```

## Main Modules

| Module | Purpose |
| --- | --- |
| `harborrag_adapters.connectors` | Source connectors that discover `SourceRecord`s and load `RawDocument`s. |
| `harborrag_adapters.parsers` | Parser factory and format parsers that produce `ParsedDocument`s. |
| `harborrag_adapters.repositories` | Tenant-isolated vector, graph, cache, object, database, and workflow-state repositories. |

See the module READMEs for deeper notes:

- `src/harborrag_adapters/connectors/README.md`
- `src/harborrag_adapters/parsers/README.md`

## Quick Start

Load local files and parse them through the default parser registry:

```python
from harborrag_adapters.connectors import (
    ConnectorQuery,
    LocalFileConfig,
    LocalFileConnector,
)
from harborrag_adapters.parsers import HarborParser

connector = LocalFileConnector(
    LocalFileConfig(
        source_path="docs",
        allowed_extensions={".md", ".txt", ".pdf"},
    )
)
parser = HarborParser()

for raw_document in connector.load_raw_documents(ConnectorQuery(recursive=True)):
    parsed = parser.parse(raw_document)
    print(raw_document.source, parsed.parser_name, len(parsed.content))
```

Create a connector through the provider registry:

```python
from harborrag_adapters.connectors import HarborConnector, GitHubRepositoryConfig

connector = HarborConnector(
    "github",
    config=GitHubRepositoryConfig(
        owner="example",
        repo="knowledge-base",
        branch="main",
        root_path="docs",
    ),
)

records = list(connector.discover())
```

## Connectors

Available connector providers:

| Provider | Config class | Source |
| --- | --- | --- |
| `local` | `LocalFileConfig` | Local file or directory trees. |
| `github` | `GitHubRepositoryConfig` | GitHub repository files through the REST API. |
| `confluence` | `ConfluenceSpaceConfig` | Confluence Cloud or Data Center pages, comments, and attachments. |
| `jira` | `JiraProjectConfig` | JIRA Cloud or Data Center issues, comments, changelog, and attachments. |
| `sharepoint` | `SharePointSiteConfig` | SharePoint document libraries through Microsoft Graph. |

Connector flow:

1. `discover(query)` returns lightweight `SourceRecord` objects.
2. `load(record)` fetches one `RawDocument`.
3. `load_raw_documents(query)` combines both steps for simple callers.

The connectors include source-level safeguards: same-origin download checks,
provider-aware retry delays, file-size gates, nested collection caps, scoped local
path handling, and structured logging.

## Parsers

`HarborParser` is the parser factory and registry. It routes by filename suffix
and MIME content type. Generic transport MIME types defer to a specific suffix;
other conflicting routes raise instead of choosing a parser silently.

Default parser support includes:

| Parser | Formats |
| --- | --- |
| `PythonPptxPresentationEngine` | `.pptx`, `.pptm` |
| `DocxDocumentEngine` | `.docx` |
| `ExcelSpreadsheetEngine` | `.xls`, `.xlsx`, `.xlsm`, `.xltx`, `.xltm` |
| `HarborPDFParser` | `.pdf` with PyMuPDF, Docling, LiteParse, MinerU, and PaddleOCR engines |
| `CsvSpreadsheetEngine` | `.csv`, `.tsv` |
| `OcrImageEngine` | OCR for common raster image formats |
| `HtmlMarkupEngine` | `.html`, `.htm`, `.xhtml` |
| `EpubDocumentEngine` | `.epub` |
| `JsonStructuredEngine` | `.json`, `.jsonl`, `.ndjson` |
| `MarkdownMarkupEngine` | `.md`, `.markdown`, `.mdx` |
| `PlainTextEngine` | Plain text and common source/config file extensions |

## Repositories

Repository backends are asynchronous context managers and take a
`StorageOperationContext` on every data operation. The context supplies the
tenant boundary; callers must not encode tenant IDs into user keys themselves.

| Family | Providers | Main products |
| --- | --- | --- |
| Vector | Qdrant | Collections, point CRUD, scan, dense and hybrid search. |
| Graph | FalkorDB | Node/edge CRUD and bounded subgraph expansion. |
| Cache | Memory, Redis | JSON values, tags, counters, compare-and-set, fenced locks. |
| Object store | Memory, filesystem, S3 | Streaming bodies, metadata, list/delete, presigned reads. |
| Database | SQLite, PostgreSQL | Document/chunk unit of work and transactional outbox. |
| Workflow state | SQLite, Redis | Versioned state, checkpoints, leases, fencing tokens. |

Use the family clients (`HarborVectorDBClient`, `HarborGraphDBClient`,
`HarborCacheDBClient`, `HarborObjectStoreDBClient`, `HarborDatabaseClient`, and
`HarborStateDBClient`) for provider-name construction, or instantiate a
provider backend directly when configuration is already typed.

Live, non-pytest smoke checks for Redis, FalkorDB, PostgreSQL, Qdrant, and SQLite
are documented in `tests/repositories/smoke/README.md`.

## Reliability Boundaries

Adapters should:

- Validate config and source scope early.
- Keep provider SDK imports inside provider modules.
- Respect provider retry and rate-limit hints.
- Fail clearly when optional parser dependencies are missing.
- Enforce source-level size and collection limits before loading content into
  memory.

Runtime should:

- Own ingestion concurrency and queueing.
- Own cross-source scheduling and global retry policy.
- Own chunk fan-out, backpressure, and resumable ingestion jobs.
- Introduce any future stream or spooled-file contract needed for truly huge
  documents.

## Tests

Package tests live in:

```text
packages/harborrag-adapters/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-adapters/tests
```

Run the hermetic repository suite and enforce its coverage target with:

```bash
pytest -n 0 packages/harborrag-adapters/tests/repositories/unit \
  --cov=harborrag_adapters.repositories --cov-fail-under=90
```

Keep connector tests close to provider behavior and parser tests focused on
format routing, dependency errors, and parsed output shape.
