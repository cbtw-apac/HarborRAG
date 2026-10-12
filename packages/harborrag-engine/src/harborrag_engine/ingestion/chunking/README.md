# Canonical chunking

This package turns one normalized `Document` into deterministic route and
evidence chunks. It does not parse raw connector payloads or select Markdown,
HTML, JSON, PDF, or Office parsers. Those decisions belong to capture and
normalization before this boundary.

## Package map

```text
chunking/
├── config.py           # stable profiles and source-to-profile mapping
├── schemas.py          # stable input, intermediate, result, and manifest contracts
├── errors.py           # chunking domain errors
├── identity/           # deterministic logical and revision identities
├── transforms/         # segmentation, refinement, packing, and route planning
├── records/            # record construction, validation, hierarchy, and rebinding
├── sources/            # canonical, Confluence, Jira, and extension registry
├── table/              # first-class TableArtifact classification/chunking
└── pipeline/           # candidate orchestration, result assembly, and composition
```

The internal dependency direction is:

```text
pipeline
  ├──> sources ───> transforms ───> contracts
  ├──> records ───> identity ─────> contracts
  ├──> table ─────> identity
  └──> contracts (config, schemas, errors)
```

Only `pipeline` composes the complete use case. Lower concerns must not import
it or reach sideways into unrelated concerns. The root `chunking` package is
the stable public facade; internal callers should prefer its exports over
module-specific import paths.

The runtime flow remains one direction:

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

Confluence and Jira are the maintained source strategies. `canonical` is the
source-neutral fallback for attachments and community connectors whose
normalizer already produces canonical elements. Source strategies may add
stable semantics such as Jira field boundaries or Confluence page identity;
they must not call connectors, parsers, models, or repositories.

## Adding an open-source source strategy

Implement `ChunkStrategy`, add a matching `ChunkingProfile`, map the connector
name through `ChunkingConfig.source_profiles`, and pass the strategy through
`build_chunking_service(additional_strategies=...)`. No change to
`ChunkingService` is required.

Keep strategies deterministic and return units in canonical source order.
Put reusable segmentation, refinement, packing, identity, and validation
behavior in `transforms/`, `identity/`, or `records/` rather than copying it
into a source strategy.
Raw-format fallbacks belong in parser or normalizer adapters.

## Context headers

Evidence `embedding_text` is the chunk content behind a deterministic context
header rendered by a `ChunkContextualizer` (core port; default
`records/contextualizer.py`), in the style of Docling's `contextualize`:

```text
Document: <title>
Section: <h1> > <h2> > ...
Caption: <caption>
File: <path> (<language>)
Scope: <enclosing definitions of a code split>
Part: <i>/<n>
```

Only present lines are rendered. `content` stays raw for citations, and
`search_text` keeps its compact lexical form. Header tokens are reserved out of
the packing limits per document (capped at a third of the hard maximum), so
content plus header fits the hard limit; when a header still does not fit it
is trimmed outermost-first, and dropped with `metadata["context_truncated"]`
as a last resort. Pass `reserve_context_tokens=False` to
`build_chunking_service` to pack without the reserve.

Oversized code units reach the refiner with their `language` metadata and no
forced separators; the refiner's `TextSplit.prefix` (enclosing definition
signatures) is kept as `metadata["code_scope"]`, and every forced split records
one-based `part_index` / `part_count`.

The header `format_version`, the refiner fingerprint and the token counter
feed runtime's processing identity, so changing any of them re-chunks
deliberately.
