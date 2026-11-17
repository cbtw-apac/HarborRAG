# Graph Build Configuration

Besides vectors, HarborRAG builds a knowledge graph from what it ingests: nodes for
documents, sections and the entities an LLM extracts, linked by typed relations.
[`config/graph/build.yaml`](../../../config/graph/build.yaml) decides how that graph is
built: runtime limits, summaries of parent nodes, the extraction vocabularies
(ontologies) and the policy of each tenant.

The runtime reads the file from `HARBORRAG_GRAPH_BUILD_CONFIG_PATH` (default
`config/graph/build.yaml`). When that variable is unset and the file is missing, the
built-in defaults apply, and they make no LLM calls. An explicitly set path that does
not exist is an error.

## Check the file

Validate the file and print its effective values, including every key this page
leaves out:

```bash
harborrag topology config validate
```

The output is one JSON envelope that starts with `{"ok": true, "data": {"runtime": {…`.
`--path` checks another file. `harborrag topology config apply` writes the tenant and
source policy to the control database.

## File layout

The shipped file:

```yaml
runtime:
  operation_timeout_seconds: 180.0
  llm_operation_cost_usd: "0.01"
  derived:
    enabled: true

summarization:
  enabled: false
  task_queue: harborrag-summaries
  debounce_seconds: 5
  max_wait_seconds: 60
  tenant_enabled: false
  max_description_tokens: 512
  budget:
    max_calls_per_document: 256
  input:
    max_children_per_call: 8
    max_bytes_per_call: 24000
    max_tokens_per_call: 6000

tenants:
  - tenant_id: DEFAULT
    mode: llm
    budget:
      max_concurrency: 2
      daily_token_cap: 200000000
      daily_cost_usd: "15.00"
    sources:
      - source_scope_id: harborrag-docs
```

Quote money values so they keep their exact decimal value. Unknown and duplicate keys
are rejected at load time.

## Tenants and sources

Each `tenants` entry sets the graph policy of one tenant and of its sources. A source
is named by its source scope ID, the stable ID of the content one ingestion source
covers (`--source-scope-id` on `harborrag ingest run`).

| Key | Default | Meaning |
| --- | --- | --- |
| `tenant_id` | required | Tenant the entry applies to |
| `mode` | `deterministic` | `llm` turns on entity extraction with the chat model; `deterministic` leaves it off |
| `prohibited` | `false` | Forbid LLM extraction for the tenant, whatever `mode` and its sources say |
| `spending_paused` | `false` | Refuse new LLM calls for the tenant until it is unpaused |
| `budget.max_concurrency` | `4` | Concurrent LLM jobs |
| `budget.daily_token_cap` | `2000000` | Tokens per day |
| `budget.daily_cost_usd` | `"10"` | Spending per day |
| `sources[].source_scope_id` | required | Source the entry applies to |
| `sources[].enabled` | `true` | Build the graph for this source |
| `sources[].model` | `primary` | Logical chat model of `config/models/models.yaml` used for extraction |
| `sources[].ontology` | none | Ontology name from `ontologies:` ([Extraction vocabularies](#extraction-vocabularies)) |

Facets, the filterable facts of an entity card, are also declared per source; see
[Facets](../detailed-guides/mcp-server/setup-and-integration.md#facets-the-filterable-part-of-a-card).

## Summarization

| Key | Meaning |
| --- | --- |
| `enabled` | Writes summary cards for parent nodes from their children's content |
| `tenant_enabled` | Lazy, on-demand tenant-level summaries. Opt-in |
| `model` | Logical chat model for summaries. Omit it to use the default model of `config/models/models.yaml` |
| `max_description_tokens` | Size of each generated summary |
| `budget.max_usd_per_run` | Stops the run once crossed. Needs pricing on the model deployment |
| `budget.max_calls_per_document` | Upper bound of summarization calls per document |
| `input.max_children_per_call` | Children one summarization call reads at most |
| `input.max_bytes_per_call`, `input.max_tokens_per_call` | Size of the child content one call reads at most |

## Extraction vocabularies

An ontology is the closed set of entity types and relation predicates the LLM may
emit. It is compiled into the structured-output JSON schema as literal enums, so the
model cannot return a type the file does not list.
[`config/graph/ontologies/example.yaml`](../../../config/graph/ontologies/example.yaml)
is a worked example. The shipped `build.yaml` selects none. To use one, name it under
`ontologies:`, with a path relative to `build.yaml`, and select it on a source with
`ontology:`:

```yaml
ontologies:
  example: ontologies/example.yaml

tenants:
  - tenant_id: DEFAULT
    mode: llm
    sources:
      - source_scope_id: harborrag-docs
        ontology: example
```

A source that names an ontology missing from `ontologies:` fails validation. Changing
an ontology changes the extraction profile fingerprint, so the affected documents are
extracted again instead of mixing two vocabularies.

Per-tenant graph settings live only in the `tenants:` blocks of `config/graph/build.yaml`;
they are not runtime settings, so `harborrag config import` and `/v1/admin/settings` do not
change them.
