# harborrag-core

The provider-neutral foundation of [HarborRAG](https://github.com/cbtw-apac/HarborRAG):
domain models, protocol contracts (ports), storage schemas and security helpers. Import it
when you write an adapter or type a function against HarborRAG data. It depends only on
Pydantic and performs no I/O on import.

Every other HarborRAG package depends on this one. To use HarborRAG as a library, install
the [`harborrag`](https://pypi.org/project/harborrag/) facade instead.

## Install

```bash
pip install harborrag-core
```

## Quick start

```python
from harborrag_core import Document, HarborError, RetrievalQuery, RetrievalResult
```

## What it provides

The package root exports the most used contracts:

| Group | Names |
| --- | --- |
| Documents | `Document`, `DocumentElement`, `DocumentProvenance`, `DocumentRelation`, `RawDocument`, `SourceRecord` |
| Retrieval | `RetrievalQuery`, `RetrievalResult` |
| Errors | `HarborError`, the base of every HarborRAG exception |
| Security | `URLPolicy`, `URLPolicyError`, `redact_secrets`, `redact_mapping` |

Deeper contracts stay in submodules:

| Submodule | Contains |
| --- | --- |
| `harborrag_core.ports.*` | Protocols that adapters implement, such as `ports.model_clients`, `ports.memory`, `ports.conversation` and the repository ports |
| `harborrag_core.domain.*` | Domain models shared by more than one package |
| `harborrag_core.contracts.errors` | The full exception family under `HarborError` |
| `harborrag_core.base` | `StrictModel` and `ExtensibleModel`, the validated-model base classes |

## Advanced usage

See
[Extending HarborRAG](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/developers/extending/README.md)
for how these contracts relate to adapter implementations.

## Contributing

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md) for
the workflow. Rules for this package:

- Add only stable, dependency-light contracts here, and keep provider SDKs out.
- Add protocols in `harborrag_core.ports.*` when adapters need a shared contract;
  model-client protocols live in `harborrag_core.ports.model_clients`.
- Add domain models in `harborrag_core.domain.*` only when more than one package needs
  them.
- Build validated models on `StrictModel` or `ExtensibleModel` from `harborrag_core.base`.
- Add package-wide exceptions to `harborrag_core.contracts.errors`; keep subsystem error
  families near their subsystem.

`harborrag-core` never imports another HarborRAG package. The Quality Gates workflow
enforces this with import-linter (`make import-boundaries`) and
`scripts/check_dependency_direction.py` (`make deps-check`).

Tests for this package live in `packages/harborrag-core/tests/`; run them from the
repository root:

```bash
uv run pytest packages/harborrag-core/tests
```

## License

Apache License 2.0.
