# Top-level adapter tests

This module covers adapter base contracts and the minimal cross-family
composition exposed by the adapter package.

```text
adapters/
  unit/       deterministic base-contract and cross-family checks
  README.md
```

Run the module from the repository root:

```bash
python -m pytest packages/harborrag-adapters/tests/adapters
```

Provider-specific behavior belongs to the connector, model, parser, or
repository module instead of this top-level suite.
