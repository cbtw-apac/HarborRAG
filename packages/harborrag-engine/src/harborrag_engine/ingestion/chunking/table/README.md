# Canonical table chunking

`CanonicalTableChunker` classifies core `TableArtifact` values and produces
canonical `ChunkRecord` values with exact `TableChunkLocator` provenance.

Classification precedence is large, time series, matrix, wide, long, then
small. `ChunkingPlan.table_policy` owns thresholds, key-column configuration,
row and column grouping, matrix projections, overlap, and dense-evidence caps.

Every table produces a deterministic extractive route chunk. Wide, large,
matrix, and time-series tables also produce schema chunks. Large-table evidence
is disabled by default; configured row-group, evidence-chunk, and dense-token
limits stop additional evidence without discarding the structured artifact.
