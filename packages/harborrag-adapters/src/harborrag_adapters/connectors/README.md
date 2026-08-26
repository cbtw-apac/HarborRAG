# Connectors

Connectors discover source items and load raw payloads from external systems.
They do not parse content and they do not orchestrate concurrent ingestion jobs.
Those responsibilities belong to parsers and `harborrag-runtime`.

## Contract

Every connector implements `BaseConnector`:

```python
from collections.abc import Iterator

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.schemas import ConnectorQuery


class BaseConnector:
    provider_name: str

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]: ...

    def load(self, record: SourceRecord) -> RawDocument: ...
```

The expected flow is:

1. `discover()` returns cheap `SourceRecord` objects with stable IDs, locators,
   first-class timestamps/checksums when available, and compact provider
   metadata.
2. `load(record)` performs the heavier fetch and returns a `RawDocument`.
3. `load_raw_documents(query)` is available for simple pull-through ingestion.

`SourceRecord` and `RawDocument` live in `harborrag-core` so runtime, engine, and
adapters share the same contracts.

Loaded document URLs belong in `RawDocument.source`, content types belong in
`RawDocument.content_type`, and duplicate copies should not be repeated inside
provider metadata. Every loaded metadata schema inherits `ConnectorMetadata`,
which supplies the normalized `record_id`, `title`, `checksum`, `created_at`,
and `updated_at` fields. Provider schemas add source-specific fields and inherit
the JSON-safe `to_dict()` serializer used at the `RawDocument.metadata`
boundary.

`source_system` and `metadata_schema_version` are fixed class attributes on
each provider metadata schema. They are included automatically by `to_dict()`
and are not constructor arguments.

## Providers

| Provider | Connector | Config | Notes |
| --- | --- | --- | --- |
| `local` | `LocalFileConnector` | `LocalFileConfig` | Reads files under one local file or directory scope. |
| `github` | `GitHubConnector` | `GitHubRepositoryConfig` | Reads repository blobs through the GitHub REST API. |
| `confluence` | `ConfluenceConnector` | `ConfluenceSpaceConfig` | Reads Confluence pages, optional comments, and optional attachments. |
| `jira` | `JiraConnector` | `JiraProjectConfig` | Reads JIRA issues, optional comments, changelog, and attachments. |
| `sharepoint` | `SharePointConnector` | `SharePointSiteConfig` | Reads SharePoint drive items through Microsoft Graph. |

All providers are registered in `connector_registry` and can also be created
through `HarborConnector`.

Provider-specific canonical document behavior belongs in that provider's
`document_transform.py`. A transform accepts the raw source payload, parser
result, and provider-neutral document produced by the engine, then returns the
enriched canonical document. Register its factory on
`ConnectorProviderDefinition`; runtime discovers the capability without a
provider-name branch. Providers that need no enrichment leave the factory
unset and use generic normalization.

```python
from harborrag_adapters.connectors import HarborConnector, LocalFileConfig

connector = HarborConnector(
    "local",
    config=LocalFileConfig(source_path="docs"),
)
```

## File Configuration

Applications can define multiple named connector instances in
`config/connectors.yaml` and load them through `harborrag_runtime.config`. The
runtime loader resolves environment
secret references and then constructs the same validated provider config
dataclasses shown below; adapters do not parse configuration files directly.

See [Connector Configuration](../../../../../docs/users/configuration/connector-config.md)
for the schema and loading API.

## Query Shape

`ConnectorQuery` provides shared filters:

| Field | Meaning |
| --- | --- |
| `path` | Provider-specific root, folder, project, or source path. |
| `pattern` | Provider-specific search string or glob-like pattern. |
| `recursive` | Whether child folders/pages/items are traversed. |
| `updated_after` | Incremental-sync timestamp filter when supported. |
| `limit` | Maximum number of discovered records returned by this call. |
| `include_attachments` | Per-query attachment toggle for connectors that support attachments. |
| `filters` | Provider-specific filters such as IDs, labels, file paths, or extensions. |

Examples:

```python
from harborrag_adapters.connectors import ConnectorQuery

recent_docs = ConnectorQuery(
    path="docs",
    recursive=True,
    filters={"extensions": [".md", ".pdf"]},
)

specific_github_files = ConnectorQuery(
    filters={"file_paths": ["README.md", "docs/architecture.md"]},
)

specific_jira_issues = ConnectorQuery(
    filters={"issue_keys": ["ENG-123", "ENG-124"]},
)
```

## Provider Examples

Local files:

```python
from harborrag_adapters.connectors import LocalFileConfig, LocalFileConnector

connector = LocalFileConnector(
    LocalFileConfig(
        source_path="docs",
        allowed_extensions={".md", ".txt", ".pdf"},
        follow_symlinks=False,
    )
)
```

GitHub:

```python
from harborrag_adapters.connectors import GitHubConnector, GitHubRepositoryConfig

connector = GitHubConnector(
    GitHubRepositoryConfig(
        owner="example",
        repo="knowledge-base",
        branch="main",
        root_path="docs",
        token=None,  # defaults to GITHUB_TOKEN
    )
)
```

Confluence:

```python
from harborrag_adapters.connectors import (
    ConfluenceConnector,
    ConfluenceSpaceConfig,
)

connector = ConfluenceConnector(
    ConfluenceSpaceConfig(
        space_key="ENG",
        base_url="https://example.atlassian.net/wiki",
        include_comments=True,
        include_attachments=True,
        token=None,  # defaults to CONFLUENCE_TOKEN
        email=None,  # defaults to CONFLUENCE_EMAIL for Cloud
    )
)
```

JIRA:

```python
from harborrag_adapters.connectors import JiraConnector, JiraProjectConfig

connector = JiraConnector(
    JiraProjectConfig(
        base_url="https://example.atlassian.net",
        project_keys=["ENG"],
        include_comments=True,
        include_changelog=True,
        token=None,  # defaults to JIRA_TOKEN or JIRA_API_TOKEN
        email=None,  # defaults to JIRA_EMAIL for Cloud
    )
)
```

SharePoint:

```python
from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig

connector = SharePointConnector(
    SharePointSiteConfig(
        site_url="https://tenant.sharepoint.com/sites/Knowledge",
        drive_name="Documents",
        root_path="Shared Documents",
        access_token=None,  # defaults to MICROSOFT_GRAPH_TOKEN
    )
)
```

## Safety And Scale

Connector safeguards are source-specific and intentionally conservative:

- HTTP connectors respect configured request rates and retry on transient status
  codes.
- Retry sleep uses `Retry-After` and `X-RateLimit-Reset` headers when providers
  return them.
- Authenticated downloads are restricted to the trusted source origin.
- Local connector scope is resolved before loading; symlinks are disabled by
  default and cannot escape the configured source root when enabled.
- File connectors enforce `max_file_size_bytes` before loading large payloads.
- Attachment connectors enforce `max_attachment_size_bytes` before parsing.
- Confluence and JIRA enforce caps for nested comments, attachments, and
  changelog collections.

Concurrency, global rate budgets, job checkpoints, and resumable ingestion should
be implemented in `harborrag-runtime`, not inside individual connectors.

## Package Layout

Provider packages use focused modules so connector orchestration stays small:

```text
connectors/<provider>/
  __init__.py
  config.py       # provider configuration and validation
  connector.py    # discover/load orchestration
  mappers.py      # provider payloads -> SourceRecord/metadata
  schemas.py      # typed provider metadata classes
  repository_paths.py  # repository URL and path behavior
```

Add these modules only when they remove real connector complexity:

```text
client.py         # HTTP/auth/rate-limit client
  content.py        # provider content traversal or text rendering
  filters.py        # query/config filter normalization
issues.py         # JIRA issue search and nested pagination
repository.py     # GitHub repository traversal/blob helpers
drive.py          # SharePoint site/drive traversal
filesystem.py     # Local filesystem traversal/filtering
```

## Adding A Connector

Use this shape:

```text
connectors/<provider>/
  __init__.py
  config.py
  connector.py
  mappers.py
  schemas.py
  query.py
```

Guidelines:

- Keep auth and SDK imports in `client.py` or the provider module.
- Put reusable behavior in a module named for the capability it implements.
- Put schema-to-domain conversion in `mappers.py`.
- Validate config in `config.py`.
- Inherit loaded metadata from `ConnectorMetadata` and keep provider-only fields
  in the provider's `schemas.py`; serialize with the inherited `to_dict()` at
  the connector boundary.
- Return `SourceRecord` from discovery and `RawDocument` from load.
- Do not duplicate `RawDocument.source` or `RawDocument.content_type` inside
  metadata. Discovery timestamps/checksums live on `SourceRecord`; loaded
  timestamps/checksums use the normalized `ConnectorMetadata` fields because
  the discovery record does not cross the raw-document boundary.
- Use shared exceptions from `connectors.exceptions`.
- Add provider tests under `packages/harborrag-adapters/tests/`.
- Keep test doubles in tests or fixtures; do not add production `mock`
  connectors.

## Logging

Use provider loggers under:

```text
harborrag.adapters.connectors.<provider>
```

Logs should describe the source, route, retry, skip reason, and failure boundary
without leaking tokens, secrets, or full authenticated URLs.
