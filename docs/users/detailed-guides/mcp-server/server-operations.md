# Operate the MCP server

Start and diagnose the HTTP server, manage tenant access, and provision the
read-only accounts. For a first connection, use the [MCP quick start](setup-and-integration.md).

- [Start the HTTP server and check its endpoints](#local-http-and-status-ui)
- [Issue keys and set corpus access](#tenant-bound-reader-keys-and-shared-corpus-access)
- [Find wrapper commands and environment variables](#run-the-wrapper-and-forward-server-options)
- [Build the image](#container-image) or [refresh read-only accounts](#read-only-database-and-object-store-accounts)

## Local HTTP and status UI

Start the HTTP transport in the background. It answers on `127.0.0.1` only:

```bash
scripts/deployment/mcp.sh --http
```

When the server is healthy, the wrapper prints
`HarborRAG MCP status UI: http://127.0.0.1:8010/` and how to stop it
(`scripts/deployment/mcp.sh down`).

The container joins `harborrag-data-network` and reaches Postgres, the object
store, Qdrant and the graph store by their service names there. It signs in to
Postgres as the read-only `harborrag_mcp` role and to MinIO as the
`harborrag-mcp` user. Those addresses are fixed in
`deploy/compose/docker-compose.mcp.yml` and use no TLS, which the server
accepts only when `HARBORRAG_ENV=dev`. Docker publishes the server on
`127.0.0.1`, on the port `HARBORRAG_MCP_PORT` names.

Use the [HTTP client examples](clients.md#connect-an-http-client) to connect an
assistant or IDE. For browser testing, [run a tool from the playground](#run-tools-from-the-browser).

### Checks before the HTTP server starts

The wrapper first checks that `harborrag-data-network` exists, in stdio mode
too. Without it, it says to run `scripts/deployment/dev.sh data` and exits with
status `2`. `--check` skips this check, because it runs with no network.

Then it runs `harborrag-mcp --http --preflight` once in the foreground. The
preflight loads the settings and connects Postgres, the object store, Qdrant
and the graph store the way the server does at start-up. In `api_key` mode it
also reads the reader-key table. Then it stops. When a step fails, nothing is
started and the wrapper exits with status `2`. The cause is one line:

```text
HarborRAG MCP preflight failed: ValueError: environment variable HARBORRAG_EMBED_API_KEY is not set
HarborRAG MCP HTTP server was not started. The line above says why: a missing setting belongs in env/.env.mcp or env/.env.models, an unreachable backend means the data services are not up yet ('scripts/deployment/dev.sh data'), and a refused login means the server's read-only accounts are missing ('scripts/deployment/dev.sh mcp-access').
```

When Postgres refuses the read-only role, the preflight line names the cause,
for example:

```text
HarborRAG MCP preflight failed: HarborUnavailableError: settings store is unreachable (cause: InvalidPasswordError)
```

`InvalidPasswordError` (a wrong or missing password) and
`InvalidAuthorizationSpecificationError` (a role that may not log in yet) both
mean the same step: run `scripts/deployment/dev.sh mcp-access`, then start the
server again. Compose stops earlier, with
`run scripts/deployment/dev.sh mcp-access to set POSTGRES_MCP_PASSWORD`, when
`env/.env.database` has no such value.

The wrapper also prints a `Note:` line in these cases, in stdio mode too. A
setting that has a default is never reported; left out of `env/.env.mcp`, it
uses that default.

| Note | What to do |
| --- | --- |
| `config/models/models.yaml reads ..., which env/.env.models does not set` | Copy the variables it names from `env-example/.env.models.example` and fill them in |
| `image ... was built from ... and the source has changed since` | Run the command again with `--build` |
| `image ... does not record a commit of this checkout` | The image was built before this check existed; run with `--build` once |

An image built from uncommitted changes cannot be compared, so the wrapper
says nothing about it.

If the server starts but is not healthy within `HARBORRAG_MCP_STARTUP_TIMEOUT`
seconds, the wrapper prints its last 20 log lines, stops it and exits with
status `2`. A running server comes back by itself after Docker or the host restarts.

Where the package is installed without a checkout, run the same check yourself
before starting the service:

```bash
harborrag-mcp --http --preflight
```

### Local HTTP endpoints

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `http://127.0.0.1:8010/` | **None** | Status page, Tool Playground, configuration editor |
| `http://127.0.0.1:8010/healthz` | **None** | Health check: `status`, `service`, `transport`, `mcp_path`, `authentication` and `tool_count` |
| `http://127.0.0.1:8010/mcp` | Bearer token, scope `mcp:read` | The MCP transport itself |

The first two are deliberately open so a loopback health check needs no
credential. The page never shows the token, and every tool call and
configuration change behind it needs one.

The server answers only requests addressed to `127.0.0.1`, `localhost` or
`::1`. A request with another `Host` header gets `421 Misdirected Request`, and
one with an `Origin` header from another site gets `403 Forbidden Origin`.
To serve another name, such as a reverse proxy's, list it in
`HARBORRAG_MCP_ALLOWED_HOSTS`, separated by commas. A name that is not
loopback is accepted only with reader keys (`HARBORRAG_MCP_AUTH_MODE=api_key`).

### Request limits

Each credential, the owner token or a reader key, may send at most
`HARBORRAG_MCP_RATE_LIMIT` requests per second (default `10`, also the burst)
and have at most `HARBORRAG_MCP_MAX_INFLIGHT` running at once (default `4`).
Both limits count `/mcp` and `/api/tools` together. A request over a limit is
refused at once: `/mcp` answers with JSON-RPC error `-32000`, and `/api/tools`
with `429` and a `Retry-After` header. stdio, `/healthz` and the status page
are not limited, and each server process, such as the Explorer server, keeps
its own budgets.

To raise a limit, set its variable in `env/.env.mcp` and restart the server;
`0` turns that limit off, and any value that is not `0` or a positive integer
stops the launcher.

### Run tools from the browser

The Tool Playground on the status page runs any tool by hand. Each step
unlocks the next:

1. Open `http://127.0.0.1:8010/`.
2. Under **Connect**, enter the owner token from `env/.env.mcp`, or a reader
   key issued by `harborrag auth keys create` (server in `api_key` mode), and
   select **Connect**. In `api_key` mode the owner token is accepted only when
   `HARBORRAG_MCP_READER_TENANT_ID` names one tenant.
3. Under **Choose a tenant**, pick one. A token bound to one tenant gets only
   that tenant: `HARBORRAG_MCP_READER_TENANT_ID` for the owner token, or the
   key's own tenant for a reader key. An unbound owner token gets every tenant
   that holds content, and **Other tenant...** to type another. A single tenant
   is chosen for you.
4. Pick a tool from the list. Its required arguments are shown, and the others
   are under **More options**.
5. Fill in the arguments and select **Run tool**. The tenant from step 3 is
   sent as `tenant_id`, so the form does not ask for it. The result appears in
   the panel on the right.

For an unbound owner token, the list holds every tenant that has a source
scope in the database. It also holds the tenants from the server settings:
`HARBORRAG_TENANT_ID` (`DEFAULT` when unset),
`HARBORRAG_CORPUS_SHARED_TENANT_ID` and the tenants that
`config/mcp/server.yaml` overrides. If the database does not answer within two
seconds, the list holds the settings tenants only and the server logs a
warning.

The page loads that tenant's effective catalog and builds argument controls
from each tool's JSON schema. A tool run goes through the same configuration,
policy, access and audit checks as the MCP transport. Results are shown as
formatted text, never injected as HTML, and retrieved content appears only
after an authenticated caller runs a tool.

### Configure tools from the browser

1. Open `http://127.0.0.1:8010/`.
2. Under **Connect**, enter the owner token, the value of
   `HARBORRAG_MCP_BEARER_TOKEN`, and select **Connect**.
3. Select **Configuration** at the top of the page. The editor loads the
   current settings, and **Load** reads them again.

The editor shows the validated JSON form of `config/mcp/server.yaml`. **Save**
writes it back as YAML in one atomic step; **Reload YAML** discards unsaved
changes and reloads the file. What the file controls is in
[Tool configuration](tool-configuration.md#tool-configuration).

The wrapper mounts `config/` read-only in the container. There the page
disables **Save** and says so, and `PUT /api/config` answers `409`. Edit
`config/mcp/server.yaml` in the checkout and select **Reload YAML**. **Save**
works where the server can write its configuration file, such as
`harborrag-mcp --http` from an installed package.

## Tenant-bound reader keys and shared corpus access

For anything beyond one developer's loopback session, do not share the owner
token. Give each person or workload its own **reader key**: a secret
bound to one tenant, with an expiry, that can list and run tools but cannot
change the server.

### Issue, list and revoke reader keys

1. Set `HARBORRAG_MCP_AUTH_MODE=api_key` in `env/.env.mcp` and restart the
   server.
2. Inside your repository checkout, create a key. `auth keys create` requires
   `--tenant`, `--owner` (`user-<name>`, `svc-<name>` or `team-<name>`),
   `--name` (the device or workload) and `--expires-in`. `--tenant` names the
   tenant the key may read and never defaults, so a mistyped tenant cannot mint
   a key in the wrong one:

   ```bash
   uv run harborrag auth keys create \
     --tenant engineering --owner user-alex --name alex-laptop --expires-in 7d
   ```

   The secret is written to `~/.harborrag/keys/alex-laptop.key`; the directory
   is created with mode `0700` when it is missing. `--secret-output` picks
   another new file, and `--migrate` applies pending control-plane migrations
   on a stack whose API has not started yet.

List a tenant's keys, revoke one key by its ID, or revoke every active key of
one owner in a tenant:

```bash
uv run harborrag auth keys list --tenant engineering
uv run harborrag auth keys revoke --key-id 0123456789abcdef01234567 --reason "laptop lost"
uv run harborrag auth keys revoke --tenant engineering --owner user-alex --reason "left team"
```

### How reader keys work

- A key looks like `hrk_<env>_v1_<key_id>.<secret>`. Only its SHA-256 hash is
  stored, in the control database table `mcp_api_keys`, with the tenant, the
  owner, an expiry and who issued it. The secret is written once to the
  output file (created exclusively, mode `0600`) and never printed; deliver it
  through your secret-sharing tool and delete the file.
- Every lifetime is bounded: at least 1 hour, and at most 7 days when
  `HARBORRAG_ENV=dev`, 30 days in `staging` and 90 days in `prod`. On a dev
  stack, `--expires-in 30d` is refused and the CLI prints the rule.
- A key is bound to the environment it was issued in and refused elsewhere.
  `mcp.sh` starts the server with the API's `HARBORRAG_ENV` from `env/.env.api`
  for that reason.
- The server looks the key up on **every** request and caches only a key ID
  it found missing, for 5 seconds, so a key created or revoked by the CLI takes
  effect on the next call, on every replica, without a restart. If the
  database cannot be reached, the request is refused.
- The tenant comes from the stored key, never from the client: a call naming
  another `tenant_id` is refused. In the status UI a reader key can load and
  run its own tenant's tools; the configuration editor and API stay
  owner-only.
- In `api_key` mode the owner token from `env/.env.mcp` keeps working beside
  the keys for that owner-only part, but only when
  `HARBORRAG_MCP_READER_TENANT_ID` names one tenant. The token never expires
  and cannot be revoked, and a TLS proxy forwards remote callers to the
  loopback listener, so it is not allowed to read every tenant. If the
  variable is unset or `*`, the server starts with reader keys only and logs a
  `WARNING`; set `HARBORRAG_MCP_READER_TENANT_ID=<tenant>` and restart to get
  the configuration editor back. Leave `HARBORRAG_MCP_BEARER_TOKEN` empty on a
  deployment that must have no owner over HTTP.
- Creating and revoking keys is recorded in the `activity` table. Rotation is
  create a new key, switch the client, revoke the old one; nothing rotates
  keys automatically.

### Where the CLI stores keys

`harborrag auth keys` writes to the same control database as the API, with the
owner credentials. Like every HarborRAG command, it runs inside a repository
checkout: it derives the database address from `env/.env.database` and takes
`HARBORRAG_ENV` from `env/.env.api`, unless `HARBORRAG_CONTROL_DB_URL` or
`HARBORRAG_ENV` is exported. It warns when it would fall back to the local
SQLite default, which no MCP server reads.

The older file-backed key list, `config/mcp/keys.yaml` with hashes in
environment variables, still works with `HARBORRAG_MCP_KEY_STORE=file`, but new
deployments should not use it. The repository ignores that file; copy
`config/mcp/keys.example.yaml` to start one. The server validates the file
strictly at startup and reads it again whenever it changes:

- On a re-read, an entry it cannot use (for example, a new key whose hash
  variable this process was not started with) is skipped with a `WARNING`;
  the other keys keep working.
- If the file is missing, empty or cannot be parsed, every key is denied and an
  `ERROR` is logged until it parses again, so a revocation saved with a typo
  never leaves the revoked key working.

### Shared corpus access

`HARBORRAG_CORPUS_ACCESS_MODE=source_acl` is the code default: a caller reads
only the documents whose current source and document permission snapshots allow
it. The shipped `env-example/.env.mcp.example` sets `tenant_shared` for the
`DEFAULT` tenant, so every caller of that tenant reads its whole published
corpus. Use `tenant_shared` only for a tenant whose published corpus is
intentionally shared among all its readers, and name that tenant with
`HARBORRAG_CORPUS_SHARED_TENANT_ID`. Other tenants keep `source_acl`, even in
the same process. Remove the two lines to return `DEFAULT` to `source_acl`.

A reader key's tenant must match each tool call's `tenant_id`; a caller cannot
select another tenant. Both HTTP modes bind to loopback; put TLS and any remote
access at a reverse proxy on the same host, and list the proxy's host name in
`HARBORRAG_MCP_ALLOWED_HOSTS`.

The owner token (`HARBORRAG_MCP_BEARER_TOKEN`) authenticates a connection but
does not grant corpus access by itself. On the shared tenant it reads the whole
corpus when a tool call names that tenant. On any other tenant it reads only
what its principal holds permissions for, which is nothing until permissions
are imported. Restart the MCP server after you change the corpus lines; a
running server keeps its previous access policy.

`HARBORRAG_MCP_READER_TENANT_ID` limits what the owner token reads, not what it
may administer: the owner keeps the configuration API and the browser editor,
and a request that names no tenant (a catalog load or a tool call) is bound to
that tenant instead of being refused. A tool call naming another tenant is
still rejected with `403`.

## Run the wrapper and forward server options

The wrapper runs the server in the `harborrag-mcp` container from
`deploy/compose/docker-compose.mcp.yml` and builds the image the first time.
The container joins the Docker network of the data services, so start those
first. Only `--check` runs without them.

| Command | Behavior |
| --- | --- |
| `mcp.sh [OPTION...]` | Stdio server (`docker compose run -T`); options are forwarded to `harborrag-mcp` |
| `mcp.sh --check [OPTION...]` | One-off handshake that prints the advertised tools |
| `mcp.sh --http` | Checks the configuration, then starts the HTTP server in the background and waits for its health check |
| `mcp.sh down` / `mcp.sh logs` | Stops, or shows the logs of, the background HTTP server |
| `mcp.sh --build ...` | Rebuilds the image first; use it after source or dependency changes |

Forwarded options are the `harborrag-mcp` flags (`--config`, `--transport`,
`--env-file`, ...). Paths refer to the container, where the checkout's
`config/` is mounted read-only at `/app/config`. The HTTP port and path come
from `env/.env.mcp`.

On a server where the package is installed instead of a checkout, run the
command directly, without the wrapper or checkout-specific variables:

```bash
harborrag-mcp --http --env-file /etc/harborrag/reader.env --config /etc/harborrag/mcp.yaml
```

### Override launcher settings with environment variables

| Variable | Redirects |
| --- | --- |
| `DATABASE_ENV_FILE` | `env/.env.database` |
| `MODEL_ENV_FILE` | `env/.env.models` |
| `MCP_ENV_FILE` | `env/.env.mcp` |
| `API_ENV_FILE` | `env/.env.api`, read only for `HARBORRAG_ENV` |
| `HARBORRAG_MCP_IMAGE` | The image tag (default `harborrag-mcp-mcp`) |
| `HARBORRAG_MCP_STARTUP_TIMEOUT` | Seconds `--http` waits for the health check (default `120`) |

The database file supplies the Compose variables that build the backend
addresses and the server's read-only credentials; the MCP file is required and
the model file is optional. Compose reads the files as data, so no shell code
in an env file runs. A model variable that no file sets is named before the
server starts; see
[Checks before the HTTP server starts](#checks-before-the-http-server-starts).

## Container image

Build and check the MCP image from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .
docker run --rm harborrag-mcp --check
```

The image contains the runtime with the reader extras, the packaged prompt
templates and the repository's `config/` folder, including
`config/models/models.yaml` and `config/mcp/server.yaml`. It runs as the
non-root `harborrag` user, keeps the audit log under that user's home
directory, and uses stdio by default.

An MCP client that starts the container must keep stdin open with `-i` and
provide the protected model environment, a `HARBORRAG_CONTROL_DB_URL` for a
read-only role, read-only object-store keys, and reachable data service
addresses; see
[Read-only database and object-store accounts](#read-only-database-and-object-store-accounts).

The checked-in HTTP launcher publishes its port on `127.0.0.1` only, on
purpose. Run `scripts/deployment/mcp.sh --http` for the status page and
Tool Playground; do not publish an unauthenticated or remotely reachable
development MCP endpoint from a container.

## Read-only database and object-store accounts

The MCP server is the most exposed HarborRAG process: it takes remote reader
keys and tool arguments that a model wrote. So it reads with two accounts of
its own and never gets the Postgres owner or the MinIO root credentials.

| Account | Store | What it may do | Password in `env/.env.database` |
| --- | --- | --- | --- |
| `harborrag_mcp` role | Postgres control database | `SELECT` on the corpus tables, `mcp_api_keys`, `setting_overrides` and `setting_revisions`, and on the tables later migrations add. No `INSERT`, `UPDATE`, `DELETE`, DDL or row locks, and every session is read-only | `POSTGRES_MCP_PASSWORD` |
| `harborrag-mcp` user | MinIO | Read objects in `harborrag-artifacts` (`s3:GetObject`, `s3:ListBucket`); nothing in `harborrag-raw`, no writes or deletes | `MINIO_MCP_PASSWORD` |

The role cannot read the control-plane tables that hold secrets, connections,
people, conversations or jobs; the server never reads them.
`deploy/compose/postgres/mcp-reader-role.sql` lists them, and
`deploy/compose/minio/mcp-reader-policy.json` is the MinIO policy.

### Create or refresh the accounts

Run this after the stack has started, and again after every upgrade:

```bash
scripts/deployment/dev.sh mcp-access
```

It prints:

```text
Granting the MCP server read-only access to PostgreSQL and MinIO...
The MCP server reads as harborrag_mcp in PostgreSQL and harborrag-mcp in MinIO.
Start or restart it to use them: scripts/deployment/mcp.sh --http
```

A password that is still blank, as in an `env/.env.database` older than these
accounts, is generated first, with a line such as
`Generated POSTGRES_MCP_PASSWORD in env/.env.database.` The command runs
the role script as the Postgres owner inside the `postgres` container and
`mc` inside the `minio` container, with the passwords passed through the
environment and pipes, never on a command line. Every step is safe to repeat.

- **Why after every upgrade.** Tables that later migrations create are
  readable by default, and a rerun applies the new release's list of tables
  the role must not read.
- **A fresh database volume** runs the role script on its first start, without
  a password, so the role exists but cannot log in until `mcp-access` runs.
- **To change a password,** edit its value in `env/.env.database`, run
  `scripts/deployment/dev.sh mcp-access`, then restart the server with
  `scripts/deployment/mcp.sh down` and `scripts/deployment/mcp.sh --http`.

Outside a checkout, create the same accounts yourself:

1. Run the role script with `psql` 15 or later, as the role that runs the
   migrations, with `POSTGRES_MCP_PASSWORD` set in its environment.
2. Create a MinIO user and attach a policy made from the policy file.
3. Give the server a `HARBORRAG_CONTROL_DB_URL` for `harborrag_mcp` and that
   user's object-store keys.

## Security boundaries

- **Read-only accounts.** The server reads Postgres as `harborrag_mcp` and
  MinIO as `harborrag-mcp`; see
  [Read-only database and object-store accounts](#read-only-database-and-object-store-accounts).
- **No secrets key.** The server is not given
  `HARBORRAG_SECRETS_ENCRYPTION_KEY`, so it cannot decrypt stored connector
  credentials.
- **Loopback only, with one exception.** The server refuses to bind anything
  but `127.0.0.1`, `localhost` or `::1`, because beyond loopback its tokens
  travel over plain HTTP. The exception is a development container: with
  `HARBORRAG_ENV=dev`, `HARBORRAG_MCP_ALLOW_INSECURE_BIND=true` and
  `HARBORRAG_MCP_ALLOWED_HOSTS`, the server listens on every interface of the
  container's own network. The launcher's Compose file sets these, and Docker
  publishes the port on `127.0.0.1` only. Do not set them for a server you
  start outside a container, and do not widen the published port. The owner
  token is for local development only: while it is the only credential, the
  host list may hold only loopback names.
- **Host and Origin are checked.** The HTTP server refuses a request addressed
  to another host name (`421`) or sent from another origin (`403`), which
  stops a web page from reaching it through DNS rebinding. A reverse proxy
  that forwards its own host name must be listed in
  `HARBORRAG_MCP_ALLOWED_HOSTS`, which needs reader keys.
- **Local stdio is the one unauthenticated path.** It opens no listener and
  must be selected explicitly. Every other transport fails closed without an
  authentication provider.
- **Remote or production HTTP requires** TLS, a production JWT/JWKS token
  verifier, and tenant and capability authorization for every service-backed
  tool.
- **Retrieved source text and chat input are untrusted data.** Tool
  descriptions and stored prompt templates contain only developer-written
  instructions.

Every call passes a capability check, JSON-schema validation, argument budgets,
result and output budgets, and an owner-only JSONL audit at
`.harborrag/mcp-audit.jsonl`. The `harborrag-mcp` launch changes the path with
`HARBORRAG_MCP_AUDIT_PATH`; a server you build in Python takes its path from the
`audit=McpAuditLog(path=...)` argument and ignores the variable. Audit
records hold a principal identifier, a digest of the arguments and the
outcome, never the bearer token or raw arguments. See
[Policy and audit status](README.md#policy-and-audit-status) for the full
sequence.
