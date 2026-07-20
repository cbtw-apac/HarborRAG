# Developer Documentation

Everything needed to understand, extend, test, and deploy HarborRAG.

## Quick navigation

- [Architecture Overview](architecture/README.md) — package map, dependency direction, and a tour of `harborrag-core`'s domain, model contracts, storage schemas, and security helpers.
- [Extending HarborRAG](extending/README.md) — implement a connector, parser, model adapter, or repository behind the provider-neutral contracts.
- [Testing](testing/README.md) — package-local test layout, markers, and the 90% coverage gate.
- [Deployment](deployment/README.md) — the `deploy/` Compose stacks and helper scripts.

For setup, branching, and the PR checklist, see the root [CONTRIBUTING.md](../CONTRIBUTING.md) — that file is the canonical source for contribution workflow.

## Coding standards

These apply on top of the architecture rules in [CONTRIBUTING.md](../CONTRIBUTING.md#architecture-rules); they're about how code is written inside a package, not which package it belongs in.

### Class and type conventions

- Prefer the established Pydantic schemas for validated public data and `@dataclass(slots=True)` for internal data carriers. Use immutable value types where identity or configuration must not change.
- Prefer `typing.Protocol` for structural contracts that multiple unrelated classes satisfy (see `harborrag_core.models.protocols`); use `abc.ABC` + `@abstractmethod` for the concrete base classes adapters subclass (see `harborrag_adapters.*.base`).
- Don't override `__new__` unless implementing a strict singleton or working with an immutable type — initialization belongs in `__init__` or a dataclass field default.

### Dependency injection

Pass the specific value or sub-config a class needs, not a large settings object:

```python
# Avoid: monolithic config passing
def __init__(self, config: EngineConfig):
    self.chunk_size = config.chunking.chunk_size

# Prefer: granular injection
def __init__(self, chunk_size: int):
    self.chunk_size = chunk_size
```

This is why `EngineConfig`, `EnginePolicy`, `McpToolPolicy`, and similar types are small, focused dataclasses rather than one large settings object.

### RAG-specific practices

- **Metadata hygiene** — every `Document` should carry a consistent `DocumentProvenance`; when you add a provenance field, update the normalizer and repository payload mappings together.
- **Provenance** — preserve the connector/parser source of truth (URL, file path, locator) so retrieval results can cite back to it.
- **Embedding compatibility** — vectors from different embedding models or dimensions are not interchangeable; a real vector repository adapter should key collections by embedding provider + dimension, not assume one global collection.

### Logging and observability

Use the telemetry interface owned by the relevant adapter or runtime boundary rather than scattering ad hoc `print` calls through orchestration code. Route anything that might contain a secret through `harborrag_core.security.redaction.redact_secrets` before logging it.

## Related

- [What is HarborRAG?](../getting-started/what-is-harborrag.md) — project overview and current status.
- [CLI Reference](../users/cli-reference/README.md) and [MCP Mock Tools](../users/detailed-guides/mcp-server/README.md) — the current user-facing surfaces.
