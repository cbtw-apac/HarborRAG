# Deployment notes

Contributor notes for the files under `deploy/`: what each folder holds, and the deployment
constraints that no single file makes obvious. How to run the stacks, which service reads
which environment file, and what a production deployment still needs are in
[Deployment](../docs/developers/deployment/README.md).

## Folders

- `compose/` - the Docker Compose stacks: data services, Temporal and the worker (with a GPU
  variant), the API, the MCP server, the Explorer MCP UI and monitoring.
- `docker/` - the Dockerfiles for the API, CLI, MCP server, Explorer MCP UI and the
  ingestion worker (with a GPU variant).
- `grafana/`, `loki/`, `prometheus/` - provisioning for the monitoring stack, independent of
  how the stack is run.
- `temporal/` - the local Temporal server's dynamic configuration; its
  [README](temporal/README.md) describes HarborRAG's Temporal settings.
- `aws/` - empty placeholders for cloud infrastructure-as-code; see its
  [README](aws/README.md).

## Run the API as exactly one process

The API's live ingestion event stream (server-sent events) and its background progress
bridge are built on `InProcessEventBus` (`harborrag_runtime.events.in_process`), an
in-memory publish/subscribe channel that exists once per process. So:

- Do not pass `--workers N` to uvicorn, and do not run more than one replica of the `api`
  service behind a shared endpoint. `docker/Dockerfile.api` and `compose/docker-compose.yml`
  already follow this: one `api` service and no `--workers` flag. Keep it that way until an
  out-of-process event bus, such as Redis publish/subscribe, replaces `InProcessEventBus`.
- If this is violated, a client connected to process A never sees a live event that process
  B publishes. It gets only the database-backed backlog on its next reconnect, so with two
  replicas ingestion progress appears to stall for about half of the connected clients.

The API lifespan in `harborrag_app/api/app.py` protects data correctness on its own. Each tick
of the ingestion progress bridge (`AppService.sync_ingestion_progress`) is gated by a
database-backed lease (control-plane migration `0017`, `singleton_leases`). If several
processes run by accident, only the lease holder appends progress events; the others do
nothing instead of diffing the same tasks and racing each other into duplicate progress rows.
The lease does not close the event fan-out gap: a client on a process that does not hold the
lease still sees only the backlog on reconnect.

Scaling the API horizontally, with every process seeing every live event, waits for a
Redis-backed `EventBusPort` implementation. Until then, scale the ingestion worker, whose
replicas already run independently, instead of the API.

## Keep the images on the shared glibc base

The API, CLI, MCP and worker images, and the GPU worker image, start from
`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`; the Explorer MCP UI image builds on the MCP
image. The base must stay a glibc (Debian) image: the locked `temporalio`, PyTorch,
TorchVision and ONNX Runtime releases publish manylinux wheels only, so the musl (Alpine)
variants cannot be used. uv ships in the base image, so nothing installs it with pip.

`tests/test_release_images_use_lock.py` checks that the API, CLI, MCP and worker images share
this base and install their dependencies from the frozen lock. The tag floats, so uv and
Python patch versions can change between builds, while application dependencies stay
reproducible through `uv export --frozen` against `uv.lock`. CI does not build these images.
