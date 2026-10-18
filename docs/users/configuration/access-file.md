# Access File (Beta Access)

The access file decides who may read which datasources. It is a beta access control: a short
list, written by an operator, of the people allowed to use a deployment and the datasources
each of them may search. It is applied on the HTTP API and on the MCP server, so chat, the
agent, retrieval, the graph routes and the MCP tools all read the same file.

It is not a per-document ACL. Everyone allowed a datasource sees all of it; see
[What it does not do](#what-it-does-not-do).

## Turn it on

Point `HARBORRAG_ACCESS_CONFIG_PATH` at the file, in the environment of the API and of the MCP
server, together with two memory settings that the file requires. The default memory recall
scopes include `project` and `tenant`, so setting the path alone makes the settings refuse to
load:

```bash
HARBORRAG_ACCESS_CONFIG_PATH=config/access.yaml
HARBORRAG_MEMORY_RECALL_SCOPES=user,session
HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE=false
```

See [Settings the file requires](#settings-the-file-requires) for why, and for the other
settings it checks.

Unset, there is no access file and nothing described on this page applies. Set, every call is
checked against the file, and a missing or broken file refuses everyone (see
[Fail closed](#fail-closed)). The file is applied only in a process that has the variable: the
CLI, for example, is held to the file only when it runs with `HARBORRAG_ACCESS_CONFIG_PATH` set,
and then needs the listing described under
[Operator principals to list](#operator-principals-to-list).

## The file

```yaml
version: 1
tenants:
  acme:
    projects:
      beta-all:
        datasources: [confluence-eng, jira-cpm]   # connection ids from config/connectors.yaml
        members:
          - principal: "user-6d2f…e91a"           # user-<the identity provider's permanent id>
            label: alice@acme.com                 # for people only, never matched
      hr-pilot:
        datasources: [confluence-hr]
        members:
          - principal: "user-b07c…41d3"
            label: carol@acme.com
```

A tenant holds projects. A project names the datasources it grants and the members who get
them. A datasource is a connection id: a key under `connectors` in
[`config/connectors.yaml`](connector-config.md). A member may belong to several projects and
reads the union of their datasources.

| Field | Meaning |
| --- | --- |
| `version` | Required; `1` |
| `tenants.<tenant>.projects.<project>.datasources` | Required list of connection ids the project grants |
| `...members` | Required list of the project's members; may be empty |
| `...members[].principal` | Required. `user-<permanent id>`, matched exactly against the caller's identity; see [Identity](#identity) |
| `...members[].label` | Optional note for a person, such as an email address. Never matched, never logged |
| `...members[].datasources` | Optional narrowing, see below |

A member gets every datasource of the project. To give one member less, list the connections
they may read:

```yaml
members:
  - principal: "user-6d2f…e91a"             # all of beta-all
  - principal: "user-9a41…0c77"
    datasources: [jira-cpm]                 # only this one
```

The narrowing can only remove: it may name only connections the project lists.

### What the loader refuses

The file is checked as a whole when it loads, and any of these makes it invalid:

- an unknown key at any level, or a `version` other than `1`
- a connection that is not in `config/connectors.yaml`, or that the connection's own `tenants`
  list does not allow for this tenant
- a member `datasources` entry that is not also in the project's `datasources`
- a principal that is blank, longer than 255 characters, or listed twice in one project
- a connection id repeated within one `datasources` list
- `datasources: null`, or an empty `datasources:` key, which would otherwise read as "no
  narrowing" and widen the member to the whole project

Because logs name only the type of the error and the path (see below), check a file before you
deploy it. From an environment where `harborrag-runtime` is installed:

```bash
python - <<'EOF'
from harborrag_runtime.config import load_connector_catalog
from harborrag_runtime.config.access_file import load_access_file

load_access_file("config/access.yaml", catalog=load_connector_catalog("config/connectors.yaml"))
print("access file is valid")
EOF
```

An invalid file raises an error that names the key or connection id at fault, never a member's
principal or label.

## Fail closed

With the file on, anything it does not grant is refused:

- A user or tenant that is not in the file is refused with `403` before anything runs: chat,
  the agent, retrieval, the graph routes and the MCP tools.
- A listed user whose datasources have never been ingested has nothing to read, and is refused
  too. Only the retrieval and graph routes answer `403` for this case; in chat, the agent and
  the MCP tools the read comes back as an error or a failed tool result, not a `403`.
- A missing, invalid or unreadable file refuses everyone, until it is fixed. A file that cannot
  be read at one moment is retried on the next request.
- Logs name only the type of the error and the path of the file. They never carry its contents,
  a principal or a label, so the log tells you that the file is broken but not where. Run the
  check above to find out.

## Kill switch

The file is read again on the next request after it changes; there is no restart and no
cache to clear. Remove a datasource from a project, or remove a member, and that access is
gone on their next call. Edits to `connectors.yaml` are picked up the same way, because the
access file is valid only against it.

An edit that leaves the file invalid refuses everyone, so check the new version first (see
above) and move it into place in one step rather than saving a half-written file. To cut off
one person quickly, delete their `members` entry.

## Identity

Each person is one principal string, `user-<permanent id>`, and it is the same string on the API
and on the MCP server. List it once.

Key it on the identity provider's permanent id, never on an email address. People change names
and addresses, and an address can be reassigned. Use Entra ID's `oid`; for any other provider,
use its `sub`. Put the email in `label` if you want a reminder of whose id it is. `label` is
never matched.

- **The API.** The user is the JWT's `sub`, so the issuer must mint tokens with
  `sub = "user-<permanent id>"`: `user-<entra oid>`, or `user-<idp sub>` when that `sub` is
  lowercase letters, digits and `-`.
- **MCP.** The user is the key's owner. Issue each key with
  `harborrag auth keys create --owner user-<permanent id>`.
- **MCP file-store keys** (`HARBORRAG_MCP_KEY_STORE=file`) carry a free-form `principal_id`. Use
  the same `user-<permanent id>` string.
- **The access file.** List that same string as the member's `principal`.

Keys issued by the CLI refuse an owner that does not match `user-`, `svc-` or `team-` followed by
lowercase letters, digits and `-`. If your provider's id has other characters (uppercase letters,
or `|` as in `auth0|...`), derive a stable lowercase form, such as the lowercase `oid`, and use
that form everywhere: in the token, the key owner and the file.

While the file is on, the API requires:

- `HARBORRAG_AUTH_USER_ID_CLAIM` stays `sub`, and
- `HARBORRAG_AUTH_MODE` is not `none`,

or it refuses to start.

## Settings the file requires

Project and tenant memories are shared across everyone in the project or tenant, and their text
comes from model output over documents readers may be entitled to differently. With the file
on, the settings refuse to load if either could carry one reader's datasource into another
reader's prompt:

| Setting | Required |
| --- | --- |
| `HARBORRAG_MEMORY_RECALL_SCOPES` | Must not include `project` or `tenant`; use `user` and `session` |
| `HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE` | Must be `false` |

Two routes expose content from every datasource of the tenant and are not narrowed, so with
the file on they need the `editor` role instead of `reader`:

- `GET /v1/ingestions/{task_id}/documents` lists the task's items with their titles.
- `GET /v1/graph/conflicts` lists graph conflicts with their descriptions and node keys.

No other route is narrowed by datasource, and the rest stay at `reader`; see
[What it does not do](#what-it-does-not-do).

## Operator principals to list

Callers that are not people still have an identity, and the file refuses any identity it does
not list. Add the ones you use, in the tenants they work in:

| Caller | Principal |
| --- | --- |
| The CLI (`harborrag retrieve`, `harborrag chat`) | `harborrag-cli` |
| MCP over stdio (no token) | `local-unauthenticated` |
| The local MCP owner token | `harborrag-local` |

An MCP tool call that names no tenant runs in the tenant `local`. A wildcard token, such as the
local owner token, can reach any tenant it names, but for a call that names none it needs a
listing in `local`. Being listed in another tenant does not carry the call into `local`.

## Choosing spaces

A datasource is read as a whole, so include a Confluence space or Jira project only if every
beta user can already open it in Jira or Confluence. Keep `restricted_content` at `skip`, its
default for Confluence and Jira connectors: pages and issues that are restricted in the source
when it is scanned are then not ingested.

This is not instant. An item that was restricted in the source after it was ingested stays
readable to everyone allowed its datasource until it is retired, which happens only after
`missing_threshold` completed full scans (default 2) have missed it. Incremental syncs usually
do not notice restriction changes, so after tightening access at the source, run those full
scans. See [Source access control](connector-config.md#source-access-control).

## What it does not do

- It applies no per-document ACLs. Everyone allowed a datasource sees all of it.
- It narrows only reads of ingested content: chat, the agent, retrieval, the graph routes and
  the MCP tools. Every other route is gated by the access-file listing, the tenant and the role
  only. A listed user with the `reader` role sees, for all of the tenant's connections and not
  only their own datasources:
  - connection ids and source types (`GET /v1/connections`)
  - ingestion task metadata, including each task's `source` and `message`
    (`GET /v1/ingestions` and `GET /v1/ingestions/{task_id}`)
  - schedules and their run history (the `GET /v1/schedules` routes)
  - projects and counts (`GET /projects`, `GET /metrics/ingestion`)
  - the sources and activity lists
- The two routes that expose item titles and graph conflicts across datasources are raised to
  `editor` (see [Settings the file requires](#settings-the-file-requires)); they are the only
  ones.

## Before you turn it on: the search index

Datasource filtering uses a `source_scope_id` payload index. The derived and contextual vector
collections gain that index on their next derived projection. Until a collection has been
reprojected, filtering on it is an unindexed scan, and a Qdrant strict mode that forbids
unindexed filtering would make those searches error. Reproject the derived collections first,
then turn the file on.
