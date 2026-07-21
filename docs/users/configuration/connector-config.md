# Connector Configuration

The runtime loader reads versioned YAML containing named connector instances. Start from [`config/connectors.example.yaml`](../../../config/connectors.example.yaml).

## Supported providers

| Provider | Common aliases | Source |
| --- | --- | --- |
| `local` | `filesystem`, `files`, `local_files` | Local file or directory tree |
| `github` | `github_repo`, `github_repository` | GitHub repository |
| `confluence` | `confluence_cloud`, `confluence_datacenter` | Confluence space |
| `jira` | `jira_cloud`, `jira_datacenter` | Jira projects/issues |
| `sharepoint` | `microsoft_sharepoint`, `sharepoint_online` | SharePoint document library |

## File shape

```yaml
version: 1

connectors:
  local-docs:
    provider: local
    enabled: true
    environment:
      source_path: LOCAL_SOURCE_PATH
    settings:
      allowed_extensions: [md, txt, pdf]
      include_hidden: false
      follow_symlinks: false
      max_file_size_bytes: 104857600
```

Each key under `connectors` is an application-level name. Multiple names may use the same provider.

| Field | Meaning |
| --- | --- |
| `provider` | Required registered provider or alias |
| `enabled` | Included by `build_enabled()`; defaults to true |
| `settings` | Literal fields for the provider configuration dataclass |
| `environment` | Config-field to environment-variable mapping for non-secret values |
| `secrets` | `<config_field>_env: VARIABLE_NAME` references for secret values |

Unknown providers, fields, aliases, versions, and malformed mappings fail during loading. Provider-specific values are validated when a connector is built.

## Load and build

```python
from harborrag_runtime.config import load_connector_catalog

catalog = load_connector_catalog("config/connectors.example.yaml")
print(catalog.names(enabled_only=True))

local_docs = catalog.build(
    "local-docs",
    environment={"LOCAL_SOURCE_PATH": "docs"},
)
```

`catalog.build(name)` builds a definition even if disabled. `catalog.build_enabled()` builds only enabled definitions and returns a dictionary keyed by application-level name.

## Precedence and paths

Effective provider settings use this precedence:

```text
provider defaults < YAML settings < referenced environment < code overrides
```

```python
github = catalog.build(
    "engineering-github",
    overrides={"branch": "release", "requests_per_minute": 60},
)
```

Relative local `source_path` values written directly in YAML resolve against the YAML file's directory. An environment-backed relative path resolves against the process working directory. A code override for `source_path` also resolves from the process context.

## Secrets and dynamic values

Keep credentials out of `settings`:

```yaml
secrets:
  token_env: GITHUB_TOKEN
```

Every referenced variable must exist and be non-empty when that connector is built. Disabled definitions do not need secrets when using `build_enabled()`.

Callbacks and custom parser objects are Python values and cannot be represented safely in this YAML boundary. Supply `custom_parsers`, attachment callbacks, or file callbacks in provider config code or explicit overrides.

## Operational limits

HTTP connectors validate timeouts, retry counts, backoff, page sizes, request rates, and source-specific collection/size limits. Configuration objects are construction recipes, not live mutable controls; construct a new connector after changing settings.

Use [Testing](../../developers/testing/README.md#real-system-smoke-checks) for credentialed connector checks.
