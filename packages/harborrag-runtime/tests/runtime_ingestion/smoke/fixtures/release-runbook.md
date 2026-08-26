# HarborRAG Release Runbook

## Publication check

Confirm that the active document version in Postgres matches the candidate
version stored in both retrieval projections.

## Recovery check

If projection verification fails, leave the previous version active and enqueue
version-addressed cleanup. Never use Redis as publication authority.

Return to the [deployment guide](deployment-guide.md) for the complete
verification matrix.
