# harborrag-core

The provider-neutral foundation of [HarborRAG](https://github.com/cbtw-apac/HarborRAG):
domain models, protocol contracts, storage schemas, and security helpers. It has no
provider SDK dependencies, so importing it pulls in nothing heavy and performs no I/O.

Every other HarborRAG package depends on this one. You rarely install it alone - install
[`harborrag`](https://pypi.org/project/harborrag/) instead - but you import from it
whenever you write an adapter or type a function signature against HarborRAG data.

```bash
pip install harborrag-core
```

## What it exports

```python
from harborrag_core import Document, HarborError, RetrievalQuery, RetrievalResult
```

| Group | Names |
| --- | --- |
| Documents | `Document`, `DocumentElement`, `DocumentProvenance`, `DocumentRelation`, `RawDocument`, `SourceRecord` |
| Retrieval | `RetrievalQuery`, `RetrievalResult` |
| Errors | `HarborError` - the base of every HarborRAG exception |
| Security | `URLPolicy`, `URLPolicyError`, `redact_secrets`, `redact_mapping` |

Deeper contracts stay in submodules rather than the package root:

| Submodule | Contains |
| --- | --- |
| `harborrag_core.ports.*` | protocols adapters implement - `ports.model_clients`, `ports.memory`, `ports.conversation`, and the repository ports |
| `harborrag_core.domain.*` | domain models shared by more than one package |
| `harborrag_core.contracts.errors` | the full exception family re-exported as `HarborError` and its subclasses |
| `harborrag_core.base` | `StrictModel` and `ExtensibleModel`, the validated-model base classes |

## Contributing to this package

- Add only stable, dependency-light contracts here.
- Keep provider SDKs out of this package.
- Add protocols in `harborrag_core.ports.*` when adapters need a shared model contract;
  model-client protocols live in `harborrag_core.ports.model_clients`.
- Add domain models in `harborrag_core.domain.*` only when multiple packages need them.
- Reuse `StrictModel` or `ExtensibleModel` from `harborrag_core.base` for validated models.
- Add package-wide exceptions to `harborrag_core.contracts.errors`; keep subsystem error
  families near their subsystem.

**Hard rule:** `harborrag-core` must never import `harborrag-adapters`, `harborrag-engine`,
`harborrag-runtime`, `harborrag-app`, `harborrag-mcp-server`, or the `harborrag`
meta-package. `scripts/check_dependency_direction.py` enforces this in CI.

## Development

Tests for this package live in `packages/harborrag-core/tests/`. Run them from the
repository root:

```bash
uv run pytest packages/harborrag-core/tests
```

See [Extending HarborRAG](https://cbtw-apac.github.io/HarborRAG/docs/developers/extending/README.html)
for how contracts here relate to adapter implementations.

Licensed under the Apache License 2.0.
