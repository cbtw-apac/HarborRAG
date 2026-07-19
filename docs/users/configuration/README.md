# Configuration

HarborRAG supports versioned YAML configuration for named connector instances
and parser profiles. Engine and complete pipeline composition settings are still
constructed in code.

1. [Connector Configuration](connector-config.md) — configure local, GitHub,
   Confluence, JIRA, and SharePoint sources without putting secrets in YAML.
2. [Parser Configuration](parser-config.md) — select PDF profiles or explicit
   PDF backend chains while retaining the default parser registry.
3. [Configuration Reference](config-file-reference.md) — `EngineConfig` and
   `EnginePolicy`.
4. [Workspace / Multi-Tenancy](workspace-mode.md) — `Tenant` and
   `RequestContext`.

## Related

- [Architecture Overview](../../developers/architecture/README.md) — where
  configuration and composition fit in the package structure.
