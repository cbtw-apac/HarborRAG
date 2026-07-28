# Chunking tests

This module covers structure-aware splitters, recursive refinement, chunker
registration, metadata preservation, and deterministic chunk boundaries.

```text
chunking/
  unit/       splitter, refiner, and registry checks
  README.md
```

Run the module from the repository root:

```bash
python -m pytest packages/harborrag-adapters/tests/chunking
```

Unit tests use injected splitter factories and do not require the package's
`chunking` optional dependency. They must not download models or call external
services.
