"""Single Temporal SDK connection adapter shared by clients and workers."""

from __future__ import annotations

from temporalio.client import Client, TLSConfig
from temporalio.service import RPCError

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import RuntimeConnectionError


async def connect_temporal_client(config: TemporalRuntimeConfig) -> Client:
    """Connect with validated settings and translate low-level transport errors."""

    connection = config.connection
    tls: bool | TLSConfig | None = None
    if connection.tls.enabled:
        tls = TLSConfig(
            server_root_ca_cert=connection.tls.server_root_ca_cert,
            domain=connection.tls.domain,
            client_cert=connection.tls.client_cert,
            client_private_key=connection.tls.client_private_key,
        )
    try:
        return await Client.connect(
            connection.target,
            namespace=connection.namespace,
            identity=connection.identity,
            api_key=connection.api_key,
            tls=tls,
        )
    except (RPCError, OSError, RuntimeError) as error:
        transport = "TLS" if connection.tls.enabled else "plaintext"
        raise RuntimeConnectionError(
            f"Could not connect to Temporal gRPC frontend {connection.target!r} "
            f"using {transport}. Use a host:port endpoint (7233 for the local "
            "stack), not an http(s) URL or the Web UI port (8080), and verify "
            "that the TLS mode matches the server."
        ) from error


__all__ = ["connect_temporal_client"]
