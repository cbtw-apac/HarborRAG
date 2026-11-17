# harborrag-adapters

`harborrag-adapters` is the part of [HarborRAG](https://github.com/cbtw-apac/HarborRAG)
that talks to systems outside it. It holds the connectors that read sources such as Jira
or a folder of files, the parsers that turn files into text, the refiners that split
text too long for one chunk, the chat, embedding and reranking model clients, and the
storage repositories. You use it directly when you write or test an adapter, or call
these pieces from your own Python code.

To run HarborRAG itself, you do not install this package by hand: follow the
[Quick Start](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/quick-start.md),
which works from a clone of the repository.

## Install

```bash
pip install harborrag-adapters
```

Releases up to 2.0.0a2 predate this README, so some of what it describes may be missing
from them; see
[Install from PyPI](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/installation.md#install-from-pypi).

The base install brings `harborrag-core`, `requests`, SQLAlchemy and `aiosqlite`. That is
enough for the connectors, the plain-text, Markdown and JSON parsers, and the SQLite,
filesystem and in-memory repositories. Everything else is an extra:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `llm` | LiteLLM, Pydantic, PyYAML | the chat, embedding and reranking clients |
| `langfuse`, `opentelemetry` | the Langfuse or OpenTelemetry SDK | the model telemetry sink of the same name |
| `langchain` | `langchain-core` | the LangChain chat model wrapper |
| `chunking-code` | tree-sitter and its language grammars | splitting code at syntax boundaries |
| `chunking-tokens` | `tiktoken` | exact token counts with `TiktokenTokenCounter` |
| `chunking` | LangChain text splitters | the `recursive` refiner |
| `qdrant`, `falkordb`, `redis`, `postgres`, `s3` | one storage client each | that storage provider |
| `control-plane` | SQLAlchemy, Alembic, `cryptography` | the migrated control-plane database |
| `tables` | `pyarrow` | stored table artifacts |

Parsers for Office files, images and PDFs have their own extras, such as `parsers`,
`pdf` and `parsers-all`; see
[Add parser and PDF extras](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/installation.md#add-parser-and-pdf-extras).

```bash
pip install "harborrag-adapters[llm,parsers,chunking-code]"
```

## Quick start

This script reads up to three Markdown or text files from a folder and parses each one.
It needs only the base install. Set `ROOT` to any folder of Markdown files; the
relative path is resolved from the directory you run the script in.

```python
import asyncio
from pathlib import Path

from harborrag_adapters.connectors import ConnectorQuery, LocalFileConfig, LocalFileConnector
from harborrag_adapters.parsers import HarborParserFactory, ParseRequest

ROOT = Path("docs")


async def main() -> None:
    connector = LocalFileConnector(
        LocalFileConfig(source_path=ROOT, allowed_extensions={".md", ".txt"})
    )
    registry = HarborParserFactory().create_registry()

    for record in connector.discover(ConnectorQuery(recursive=True, limit=3)):
        result = await registry.parse_request(
            ParseRequest(
                source_uri=str(ROOT / record.locator),
                filename=Path(record.locator).name,
                mime_type=record.source_type,
            )
        )
        print(record.locator, result.parser_name, result.engine_name, len(result.text))


asyncio.run(main())
```

Each line shows the file, the parser family, the engine inside that family and the
number of characters extracted. Run against the repository's `docs` folder, it prints:

```text
developers/architecture/data-lifecycle.md markup markdown 7153
developers/architecture/projection-rebuild.md markup markdown 9249
developers/architecture/README.md markup markdown 16371
```

Discovery is synchronous and yields `SourceRecord` objects (`id`, `source_type`,
`locator`, `metadata`, `updated_at`, `checksum`). Parsing is asynchronous: the registry
picks a parser family, and the family picks the engine.

## What it provides

| Module | What it holds | Notes |
| --- | --- | --- |
| `harborrag_adapters.connectors` | `local`, `github`, `confluence`, `jira` and `sharepoint` connectors that discover and load source items | [Connectors](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/src/harborrag_adapters/connectors/README.md), [Confluence normalization](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/src/harborrag_adapters/connectors/confluence/normalization/README.md) |
| `harborrag_adapters.parsers` | `HarborParserFactory` and eight parser families: `text`, `markup`, `structured`, `spreadsheet`, `document`, `presentation`, `image` and `pdf` | [Parsers](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/src/harborrag_adapters/parsers/README.md), [PDF parsers](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/src/harborrag_adapters/parsers/pdf/README.md) |
| `harborrag_adapters.chunking` | `build_text_refiner`, which splits an oversized unit of a document at code, Markdown or sentence boundaries, plus `MarkdownChunker` and `RecursiveTextRefiner` | [Chunking configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/chunking-config.md#refiner) |
| `harborrag_adapters.models` | chat, embedding and reranking clients behind the model contracts of `harborrag-core` | [Model adapters](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/src/harborrag_adapters/models/README.md), [Chat adapter](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/src/harborrag_adapters/models/chat/README.md) |
| `harborrag_adapters.repositories` | tenant-isolated vector, graph, cache, object, database and workflow-state storage | Repositories, below |
| `harborrag_adapters.topology` | `LLMEntityExtractor`, the model adapter that extracts entities for the knowledge graph | [Graph build configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/graph-build-config.md) |

Each family keeps its own registry or factory: connectors, parsers, model providers and
repository backends are registered separately. There is no registry shared across
families, so adding one provider never means editing an unrelated family.

`build_text_refiner(token_counter)` raises `GrammarSetMismatch` when the
`chunking-code` grammars are missing or at another version; pass
`require_grammars=False` to split code at separators instead. The YAML side of this
choice is `refiner.require_code_grammars`; see
[Code splitting needs the tree-sitter grammars](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/chunking-config.md#code-splitting-needs-the-tree-sitter-grammars).

### Create a connector by provider name

`HarborConnector` builds any registered provider from its name:

```python
from harborrag_adapters.connectors import GitHubRepositoryConfig, HarborConnector

connector = HarborConnector(
    "github",
    config=GitHubRepositoryConfig(
        owner="example",
        repo="knowledge-base",
        branch="main",
        root_path="docs",
    ),
)
```

`connector.discover()` then lists the repository's files through the GitHub REST API.
The provider names, their aliases and their options are listed in
[Connector configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/connector-config.md#supported-providers).

### Repositories

Repository backends are asynchronous context managers. Every data operation takes a
`StorageOperationContext`, which carries the tenant boundary, so callers never put
tenant IDs into keys themselves.

| Family | Client | Built-in backends | What it stores |
| --- | --- | --- | --- |
| Vector | `HarborVectorDBClient` | `qdrant` | Collections, point CRUD, scan, dense and hybrid search |
| Graph | `HarborGraphDBClient` | `falkordb` | Node and edge CRUD, bounded subgraph expansion |
| Cache | `HarborCacheDBClient` | `memory`, `redis` | JSON values, tags, counters, compare-and-set, fenced locks |
| Object store | `HarborObjectStoreDBClient` | `memory`, `filesystem`, `s3` | Streaming bodies, metadata, list and delete, presigned reads |
| Database | `HarborDatabaseClient` | `sqlite`, `postgresql` | Document and chunk unit of work, transactional outbox |
| Workflow state | `HarborStateDBClient` | `sqlite`, `redis` | Versioned state, checkpoints, leases, fencing tokens |

Use a family client to build a backend by name, or construct a backend class directly
when its configuration is already typed.

## Advanced usage

- [Architecture: adapter layer](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/developers/architecture/README.md#adapter-layer)
  explains what adapters own and what runtime owns, such as concurrency, retries
  across documents and resumable jobs.
- [Model configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/model-config.md)
  describes the model catalog the `models` clients load.
- [Parser configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/parser-config.md)
  lists the formats the parsers read and the PDF engines.

## Contributing

[Extending HarborRAG](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/developers/extending/README.md)
shows where a new connector, parser, model provider or repository goes and what it must
include, and
[Adapter contributions](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md#adapter-contributions)
summarizes the rules for an adapter change. Keep fakes and test doubles under `tests/`:
production packages ship no `mock.py` modules.

Run this package's tests from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests
```

The [adapter test index](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/tests/README.md)
lists every test folder and the package-wide coverage gate, and
[Repository smoke checks](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-adapters/tests/repositories/smoke/README.md)
run against live Redis, FalkorDB, PostgreSQL, Qdrant and SQLite.

## License

Apache License 2.0.
