# Canonical chunking

Contributor notes for `harborrag_engine.ingestion.chunking`, which turns one normalized
`Document` into deterministic route and evidence chunks. It does not parse raw connector
payloads or pick Markdown, HTML, JSON, PDF or Office parsers: capture and normalization
make those decisions before this boundary. The profile settings are described in
[Chunking configuration](../../../../../../docs/users/configuration/chunking-config.md).

## Package map

```text
chunking/
├── config.py           # stable profiles and source-to-profile mapping
├── schemas.py          # stable input, intermediate, result and manifest contracts
├── errors.py           # chunking domain errors
├── anchors.py          # the one reading of a candidate's stable source anchors
├── identity/           # deterministic logical and revision identities
├── transforms/         # segmentation, refinement (incl. table row splits), packing, routing
├── records/            # record construction, validation, hierarchy and rebinding
├── sources/            # canonical, Confluence and Jira strategies and the registry
├── table/              # CanonicalTableChunker for core TableArtifact values
└── pipeline/           # candidate orchestration, result assembly and composition
```

`config.py`, `schemas.py`, `errors.py` and `anchors.py` are the contracts the other
folders share. Imports point one way:

```text
pipeline ──> sources ──> transforms ──> contracts
pipeline ──> records ──> identity ────> contracts
pipeline ──> transforms, identity, contracts
table ─────> identity, contracts
```

Only `pipeline` composes the complete use case; lower folders never import it or reach
sideways into unrelated folders. `table/` is not part of that pipeline: an oversized table
inside a document is split row by row in `transforms/`, and `CanonicalTableChunker` is
exported for callers that chunk a `TableArtifact` directly (see
[table/README.md](table/README.md)). The root `chunking` package is the stable public
facade, so callers import from it rather than from module paths.

## How a document becomes chunks

```text
canonical document
  -> source strategy
  -> structural units
  -> hard-limit refinement
  -> token packing
  -> route + evidence candidates
  -> deterministic identities and records
  -> validation and manifest
```

`ChunkingConfig.source_profiles` maps a normalized connector name to a profile, and the
profile names the strategy. Confluence and Jira are the maintained source strategies.
`canonical` is the source-neutral fallback for attachments and for connectors whose
normalizer already produces canonical elements. A source strategy may add stable
semantics, such as Jira field boundaries or Confluence page identity, but never calls
connectors, parsers, models or repositories.

## Add a source strategy

1. Implement `ChunkStrategy`, keeping it deterministic and returning units in canonical
   source order.
2. Add a matching `ChunkingProfile`.
3. Map the connector name to that profile in `ChunkingConfig.source_profiles`.
4. Pass the strategy to `build_chunking_service(additional_strategies=...)`.

`ChunkingService` needs no change. Put reusable segmentation, refinement, packing,
identity and validation behaviour in `transforms/`, `identity/` or `records/` instead of
copying it into a strategy. Raw-format fallbacks belong in parser or normalizer adapters.
To wire the strategy into production ingestion, see
[the ingestion runtime notes](../../../../../harborrag-runtime/src/harborrag_runtime/ingestion/README.md).
