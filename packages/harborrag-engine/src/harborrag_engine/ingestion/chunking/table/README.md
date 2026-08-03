# Canonical table chunking

`CanonicalTableChunker` classifies core `TableArtifact` values and produces
canonical `ChunkRecord` values with exact `TableChunkLocator` provenance.
`TableChunkingRequest` supplies explicit connector, document-kind, and bounded
source context, so the same implementation serves Confluence and Jira without
embedding provider-specific labels in the table factory.

Classification precedence is large, time series, matrix, wide, long, then
small. `ChunkingPlan.table_policy` owns thresholds, key-column configuration,
row and column grouping, matrix projections, overlap, and dense-evidence caps.

Every table produces a deterministic extractive route chunk. Wide, large,
matrix, and time-series tables also produce schema chunks. Large-table evidence
is disabled by default; configured row-group, evidence-chunk, and dense-token
limits stop additional evidence without discarding the structured artifact.
