# Canonical table chunking

Contributor notes for `CanonicalTableChunker`, which classifies a core `TableArtifact` and
produces canonical `ChunkRecord` values with exact `TableChunkLocator` provenance. It is
exported from the `chunking` package root; the document chunking pipeline does not call
it (see [the chunking notes](../README.md)).

## Inputs

`TableChunkingRequest` carries the artifact plus explicit tenant, connection, source scope,
document title, connector type, document kind and bounded source context. Because the
context arrives in the request, one implementation serves Confluence and Jira without
provider-specific labels in the table factory.

## Classification and planning

Classification checks the shapes in this order: large, time series, matrix, wide, long,
then small. `ChunkingPlan.table_policy` owns the thresholds, key-column configuration, row
and column grouping, matrix projections, overlap and the dense-evidence caps.

A small table whose evidence chunk would exceed the plan's hard maximum is planned as a
long table instead.

## What each table produces

- Every table produces a deterministic extractive route chunk.
- Wide, large, matrix and time-series tables also produce schema chunks.
- Evidence for large tables is off by default (`large_table_evidence_enabled`).
- The row-group, evidence-chunk and dense-token caps stop further evidence without
  discarding the structured artifact; the result then carries a warning.
