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

Install advanced PDF backends separately when needed:

```bash
pip install -e "packages/harborrag-adapters[pdf]"
```

## Main Modules

| Module | Purpose |
| --- | --- |
| `harborrag_adapters.connectors` | Source connectors that discover `SourceRecord`s and load `RawDocument`s. |
| `harborrag_adapters.parsers` | Parser factory and format parsers that produce `ParsedDocument`s. |

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
and MIME content type, and raises on conflicting routes instead of choosing a
parser silently.

Default parser support includes:

| Parser | Formats |
| --- | --- |
| `PptxParser` | `.pptx`, `.pptm` |
| `DocxParser` | `.docx` |
| `ExcelParser` | `.xls`, `.xlsx`, `.xlsm`, `.xltx`, `.xltm` |
| `PdfParser` | `.pdf` with PyMuPDF, Docling, LiteParse, MinerU, and PaddleOCR backends |
| `CsvParser` | `.csv`, `.tsv` |
| `ImageParser` | OCR for common raster image formats |
| `HtmlParser` | `.html`, `.htm`, `.xhtml` |
| `EpubParser` | `.epub` |
| `JsonParser` | `.json`, `.jsonl`, `.ndjson` |
| `MarkdownParser` | `.md`, `.markdown`, `.mdx` |
| `TextParser` | Plain text and common source/config file extensions |

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

Keep connector tests close to provider behavior and parser tests focused on
format routing, dependency errors, and parsed output shape.
