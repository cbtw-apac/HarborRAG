# harborrag-memory

Owns long-term and working memory for HarborRAG agents/sessions.

**Status: placeholder.** This package currently exposes no public API — `src/harborrag_memory/__init__.py` is empty and there is no `tests/` directory yet. It exists to reserve the package boundary ahead of implementation.

## Folder ownership

```text
src/harborrag_memory/
```

## Team deliverables

- Define the memory contract (what gets stored, retrieved, and forgotten).
- Implement storage/retrieval behind this package's boundary; no other package should reach into its internals.
- Add a `tests/` directory alongside the first real implementation.

## Package tests

No tests exist yet. When implementation begins, add tests under:

```text
packages/harborrag-memory/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-memory/tests
```

Keep new tests in this folder when adding or changing behavior owned by this package.
