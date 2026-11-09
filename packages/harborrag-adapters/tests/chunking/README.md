# Chunking tests

These tests cover the structure-aware splitters, recursive refinement, the chunker routing table
and default factory, metadata preservation and deterministic chunk boundaries. All of them are in
`unit/`.

Run them from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/chunking
```

The tests inject splitter factories and parser loaders, so they do not need the package's
`chunking` extra. Two kinds of test skip instead of failing when a library is missing:

- The syntax-tier code tests need `tree-sitter` and `tree-sitter-python`, which the package's
  `test` extra installs.
- The token-counter test needs `tiktoken` and its encoding.

None of these tests may download a model or call an external service.
