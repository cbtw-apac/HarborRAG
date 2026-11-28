# Chunking Configuration

Before HarborRAG embeds a document, it splits it into chunks: passages small enough
to embed and search on their own. This page describes
[`config/ingestion/chunking.yaml`](../../../config/ingestion/chunking.yaml), which sets
how large chunks are, which profile each source uses, and how a piece that is still
too large is split.

The runtime reads the file from `HARBORRAG_CHUNKING_CONFIG_PATH`. The file is
optional: when it is missing and its path was not set explicitly, the built-in
defaults apply, and they are the values the checked-in file declares. An explicitly
set path that does not exist is an error.

Every value in the file is part of the processing identity, the inputs that decide
how a document is chunked. Changing one re-chunks and re-embeds each document the
next time it is ingested. The API and the workers must read the same file, or they
disagree on that identity. `harborrag doctor` validates the file in its
`chunking policy` check. To cap chunk size per tenant, project or connection without
editing the file, use the `chunking.max_tokens`
[runtime setting](runtime-settings.md#settings-and-their-levels).

## File layout

```yaml
version: 1
configuration_version: canonical-source-policies   # bump to force a re-chunk
create_route_chunks: true                           # document/section route chunks

refiner:
  strategy: structure            # structure | recursive
  require_code_grammars: true

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

Any section you leave out keeps its default. `profiles` and `source_profiles` replace
the defaults as a whole, so list every profile you still use. Unknown keys, duplicate
keys and inconsistent limits are rejected at load time.

## Profile keys

A profile is a named set of chunking rules. Each source type uses the profile
`source_profiles` maps it to, or `default_profile`.

| Profile key | Meaning |
| --- | --- |
| `strategy` | Chunk strategy that builds units: `canonical`, `jira` or `confluence` |
| `limits.minimum_tokens` | Chunks below this are merged with a compatible neighbour |
| `limits.target_tokens` | Size the packer aims for |
| `limits.maximum_tokens` | Hard limit. A larger unit goes to the refiner |
| `limits.soft_maximum_tokens` | Packing ceiling (default `maximum_tokens`) |
| `limits.overlap_tokens` | Text repeated from the previous split; default `0`, and must stay below `target_tokens`. Code, JSON, log and table units never overlap |
| `merge_small_peers` | Join small adjacent units of one section (default `true`) |
| `preserve_sections` | Carry the heading path on every chunk (default `true`) |
| `repeat_table_headers` | Repeat column headers on every table part (default `true`) |

A unit is one structural piece of a parsed document, such as a paragraph, a table, a
list or a code block.

## Refiner

The refiner splits only a unit that is still above `maximum_tokens` after
segmentation. Each split maps back to an exact range of the source text.

`structure` (the default) routes each unit to a strategy that understands it:

| Unit | Split at |
| --- | --- |
| Code block or code symbol | Syntax boundaries (tree-sitter). Comments and decorators stay with the definition they introduce |
| Table, table row, list, JSON path, or explicit separators | Line breaks |
| Prose containing Markdown structure | Headings, fence and quote edges, list items, then prose cuts |
| Other prose | Paragraphs, then sentence ends (abbreviations and decimals are not cut), then lines, clauses and words |

A piece of a long method keeps the signatures of the definitions around it (for
example `class Foo:` / `def run(self):`) in the chunk's `enclosing_scope` metadata,
and they lead its embedding and search text, so the piece is found by the method's
name. Markdown code fences pass their language (```` ```python ````) to the splitter,
and source files (`.py`, `.ts`, `.go`, `.yaml`, ...) are parsed as code that keeps its
indentation. A document picks this up when it is parsed again: every ingestion run
after an upgrade that changes the processing identity does so, but a connector-free
reindex reuses the stored parse.

`recursive` uses the LangChain recursive character splitter, installed with
`pip install "harborrag-adapters[chunking]"`. It splits at blank lines, line breaks,
sentence punctuation and spaces, without regard to code or Markdown structure.

## Code splitting needs the tree-sitter grammars

Syntax-aware code splitting uses tree-sitter grammars, one package per language. The
worker images, the CLI image, `harborrag[local]`, `harborrag-app[production]` and
`harborrag-runtime[production]` install them; elsewhere, install them with:

```bash
pip install "harborrag-adapters[chunking-code]"
```

By default (`require_code_grammars: true`) ingestion stops at startup when a grammar
is missing or at another version, and `harborrag doctor` fails its `chunking policy`
check. The processing identity names the grammar set without looking at what is
installed, so this is what keeps code splits identical on every process that shares
the identity.

With `require_code_grammars: false`, `harborrag doctor` only warns. Missing grammars
are logged and code splits at definition keywords, blank lines and line breaks;
workers with and without grammars then chunk code differently under the same
identity, and installing the grammars later does not re-chunk what is already stored.
