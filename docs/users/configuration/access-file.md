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
- a tenant key that is not a tenant id, such as `*`: a tenant id is 1-128 ASCII letters,
  digits, `.`, `_` or `-`, beginning with a letter or digit
- a connection that is not in `config/connectors.yaml`, or that the connection's own `tenants`
  list does not allow for this tenant
- a member `datasources` entry that is not also in the project's `datasources`
- a principal that is blank, longer than 255 characters, or listed twice in one project
- a connection id repeated within one `datasources` list
- `datasources: null`, or an empty `datasources:` key, which would otherwise read as "no
  narrowing" and widen the member to the whole project

Because logs name only the type of the error and the path (see below), check a file before you
deploy it, and check it again before you deploy an edit to `config/connectors.yaml`, since the
access file is valid only against it (see [Kill switch](#kill-switch)). From an environment
where `harborrag-runtime` is installed:

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

- A user or tenant that is not in the file is refused before anything runs: chat, the agent,
  retrieval, the graph routes and the MCP tools. The HTTP API answers `403`. So do the MCP
  server's HTTP playground routes (`GET /api/tools` and `POST /api/tools/call`), with the
  message `not listed in the access file`. Over the MCP protocol the refusal is a tool error,
  and the server masks its text to `Error calling tool '<name>'`; the reason,
  `PermissionError: not listed in the access file`, is in the server log, and the MCP audit
  log records `PermissionError`.
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
gone on their next call. Nothing they created earlier is purged: their personal memories and
chat history stay, including text drawn from a datasource they have lost, and a user who is
still listed can still read their own.

Edits to `connectors.yaml` are picked up the same way, because the access file is valid only
against it. An edit there can therefore refuse everyone: removing a connection the access
file lists, or changing the connection's `tenants` so that it no longer allows a tenant the
access file lists it under, makes the whole access file invalid until one of the two files is
fixed.

An edit that leaves the access file invalid, directly or through `connectors.yaml`, refuses
everyone, so check the new versions first (see above) and move each file into place in one
step rather than saving a half-written file. To cut off one person quickly, delete their
`members` entry.

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

No other route is raised to `editor`; the reads that are not narrowed stay at `reader` (see
[What it does not do](#what-it-does-not-do)).

## Operator principals to list

Callers that are not people still have an identity, and the file refuses any identity it does
not list. Add the ones you use, in the tenants they work in:

| Caller | Principal |
| --- | --- |
| The CLI (`harborrag retrieve`, `harborrag chat`) | `harborrag-cli` |
| MCP over stdio (no token) | `local-unauthenticated` |
| The local MCP owner token | `harborrag-local` |

Your own admin or owner JWT is held to the file like any other token. List its `sub` as a
member of a project that grants at least one datasource, in every tenant you administer;
otherwise the API refuses it there, admin routes included.

On the MCP playground route `POST /api/tools/call`, and for in-process calls, a call to a tool
that takes a tenant but names none runs in the token's tenant when the token grants exactly
one, and otherwise in the tenant `local`. A wildcard token, such as the local owner token, can
reach any tenant it names, but for a call that names none it needs a listing in `local`. Being
listed in another tenant does not carry the call into `local`.

Over the MCP protocol there is no `local` fallback: a call to a tool that takes a tenant but
names none is refused outright unless the token grants exactly one tenant. With a wildcard
token, or with no token (stdio), name the tenant in the call.

`describe_graph` takes no tenant and reads none, so on every transport it needs only a listing
in some tenant.

## Choosing spaces

A datasource is read as a whole, so include a Confluence space or Jira project only if every
beta user can already open it in Jira or Confluence. Keep `restricted_content` at `skip`, its
default for Confluence, Jira and SharePoint connectors: items that are restricted in the
source when it is scanned are then not ingested.

This is not instant. An item that was restricted in the source after it was ingested stays
readable to everyone allowed its datasource until it is retired, which happens only after
`missing_threshold` consecutive completed full scans (default 2) have missed it. Incremental
syncs usually do not notice restriction changes, so after tightening access at the source, run
those full scans. See [Source access control](connector-config.md#source-access-control).

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

Datasource filtering uses a `source_scope_id` payload index. The two derived vector
collections, contextual chunks (`contextual-v2-*`) and parent descriptions (`parent-v2-*`),
gain that index the next time any derived projection is written to them, whether or not the
file is on. Each tenant has its own collections, and Qdrant builds the index over a whole
collection, so one write to each is enough. Until then, filtering them by datasource is an
unindexed scan, and a Qdrant strict mode that forbids unindexed filtering would make those
searches error.

The topology worker writes a derived projection each time it derives a newly accepted build.
To add the index before you turn the file on, run derived enrichment for one accepted build
in each tenant:

```bash
harborrag topology status --tenant acme            # lists the build_id of each accepted job
harborrag topology derive <build_id> --tenant acme
```

`derive` prints a state for its `contextual` and `parents` stages; each stage that reports
`ready` has written its collection, and so indexed it. It re-runs derived enrichment for that
build, which can call the embedding and description models. With derived enrichment off
(`runtime.derived.enabled: false` in `config/topology/graph_build.yaml`) it writes nothing and
reports `disabled`. `harborrag topology rebuild` does not add the index: it restores only the
graph projection.
