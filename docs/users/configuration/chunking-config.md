# Chunking Configuration

The runtime reads chunk sizes, per-source profiles and the oversized-unit
refiner from the versioned YAML file
[`config/chunking.yaml`](../../../config/chunking.yaml)
(`HARBORRAG_CHUNKING_CONFIG_PATH`). The file is optional. When it is missing
and its path was not set explicitly, the built-in defaults apply. They are the
same values that the checked-in file declares. An explicitly set path that does
not exist is an error.

Every value in the file is part of the processing identity. Changing one
re-chunks and re-embeds each document the next time it is ingested. The API and
the workers must read the same file, or they disagree on that identity.
`harborrag doctor` validates the file.

## File layout

```yaml
version: 1
configuration_version: canonical-source-policies   # bump to force a re-chunk
create_route_chunks: true                           # document/section route chunks

refiner:
  strategy: structure            # structure | recursive
  require_code_grammars: false

default_profile: canonical
profiles:
  canonical:
    strategy: canonical
    limits: {minimum_tokens: 120, target_tokens: 700, maximum_tokens: 1100, overlap_tokens: 0}
  jira:
    strategy: jira
    limits: {minimum_tokens: 80, target_tokens: 600, maximum_tokens: 1000, overlap_tokens: 40}
  confluence:
    strategy: confluence
    limits: {minimum_tokens: 120, target_tokens: 750, maximum_tokens: 1200, overlap_tokens: 0}

source_profiles:       # connector type -> profile
  confluence: confluence
  jira: jira
```

Any section you leave out keeps its default. `profiles` and `source_profiles`
replace the defaults as a whole, so list every profile you still use. Unknown
keys, duplicate keys and inconsistent limits are rejected at load time.

| Profile key | Meaning |
| --- | --- |
| `strategy` | Chunk strategy that builds units: `canonical`, `jira` or `confluence` |
| `limits.minimum_tokens` | Chunks below this are merged with a compatible neighbour |
| `limits.target_tokens` | Size the packer aims for |
| `limits.maximum_tokens` | Hard limit. A larger unit goes to the refiner |
| `limits.soft_maximum_tokens` | Packing ceiling (default `maximum_tokens`) |
| `limits.overlap_tokens` | Text repeated from the previous split. Must stay below `target_tokens`. Code, JSON, log and table units never overlap |
| `merge_small_peers` | Join small adjacent units of one section (default `true`) |
| `preserve_sections` | Carry the heading path on every chunk (default `true`) |
| `repeat_table_headers` | Repeat column headers on every table part (default `true`) |

## Refiner

The refiner splits only a unit that is still above `maximum_tokens` after
segmentation. Each split maps back to an exact source range.

`structure` (the default) routes each unit to a strategy that understands it:

| Unit | Split at |
| --- | --- |
| Code block or code symbol | Syntax boundaries (tree-sitter). Comments and decorators stay with the definition they introduce |
| Table, table row, list, JSON path, or explicit separators | Line breaks |
| Prose containing Markdown structure | Headings, fence and quote edges, list items, then prose cuts |
| Other prose | Paragraphs, then sentence ends (abbreviations and decimals are not cut), then lines, clauses and words |

A piece of a long method keeps the signatures of the definitions around it
(for example `class Foo:` / `def run(self):`) in the chunk's `enclosing_scope`
metadata, and they lead its embedding and search text, so the piece is found by
the method's name. Markdown code fences pass their language (```` ```python ````)
to the splitter, and source files (`.py`, `.ts`, `.go`, `.yaml`, ...) are parsed
as code that keeps its indentation. A document picks this up when it is parsed
again: every ingestion run after an upgrade that changes the processing identity
does so, but a connector-free reindex reuses the stored parse.

Syntax-aware splitting needs the tree-sitter grammars:

```bash
pip install "harborrag-adapters[chunking-code]"
```

When the grammars are missing, code is split at definition keywords, blank lines
and line breaks, and a warning is logged. Set `require_code_grammars: true` to
fail startup instead. Doing so keeps code splits identical across every
process that shares the processing identity.

`recursive` uses the LangChain recursive character splitter
(`pip install "harborrag-adapters[chunking]"`). It splits at blank lines, line
breaks, sentence punctuation and spaces, without regard to code or Markdown
structure.
