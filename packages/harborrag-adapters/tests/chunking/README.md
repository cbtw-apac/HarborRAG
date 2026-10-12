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

`refiner_contract.py` holds the invariants every refiner must satisfy
(`assert_valid_splits`); new strategies should run their outputs through it.

Unit tests use injected splitter factories and parser loaders and do not
require optional dependencies. Tests that need tree-sitter grammars or tiktoken
skip when the `chunking-code` / `chunking-tokens` extras are absent. They must
not download models or call external services.
