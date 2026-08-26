# harborrag-core

Owns provider-neutral domain models, protocol contracts, storage schemas, and security helpers.

## Team deliverables

- Add only stable, dependency-light contracts here.
- Keep provider SDKs out of this package.
- Add protocols in `harborrag_core.models.protocols` when adapters need a shared model contract.
- Add domain models in `harborrag_core.domain.*` only when multiple packages need them.
- Reuse `StrictModel` or `ExtensibleModel` from `harborrag_core.base` for validated models.
- Add package-wide exceptions to `harborrag_core.errors`; keep subsystem error families near their subsystem.

## Rule

`harborrag-core` must never import adapters, engine, runtime, app, MCP, or the meta-package.

## Package tests

Tests for this package live in:

```text
packages/harborrag-core/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-core/tests
```

Keep new tests in this folder when adding or changing behavior owned by this package.
