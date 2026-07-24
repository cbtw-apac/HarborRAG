from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.models.runtime.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.runtime.redis_config import RedisConnectionConfig
from harborrag_adapters.models.runtime.secrets import (
    AwsSecretsManagerResolver,
    AzureKeyVaultResolver,
    CompositeSecretResolver,
    EnvironmentSecretResolver,
    GoogleSecretManagerResolver,
    VaultSecretResolver,
)
from harborrag_adapters.models.runtime.secrets.base import parse_secret_reference
from harborrag_adapters.models.runtime.security import SecretReference

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class SyncRedis:
    def get(self, name: str) -> Any:
        del name
        return None

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        del name, value, kwargs
        return True

    def delete(self, *names: str) -> int:
        del names
        return 0

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        del script, numkeys, args
        return [1, "ok"]

    def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    def close(self) -> None:
        return None


class AsyncRedis:
    async def get(self, name: str) -> Any:
        del name
        return None

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        del name, value, kwargs
        return True

    async def delete(self, *names: str) -> int:
        del names
        return 0

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        del script, numkeys, args
        return [1, "ok"]

    async def hgetall(self, name: str) -> dict[str, str]:
        del name
        return {}

    async def aclose(self) -> None:
        return None


def connections() -> RedisConnectionLifecycle:
    return RedisConnectionLifecycle(
        RedisConnectionConfig(url="redis://localhost"),
        sync_client=SyncRedis(),
        async_client=AsyncRedis(),
        owns_clients=False,
    )


def reference(uri: str) -> SecretReference:
    return SecretReference(uri=uri)


def test_secret_reference_parsing_and_environment_resolver() -> None:
    parsed = parse_secret_reference(reference("secret://env/MY%5FKEY#field"))
    assert parsed.provider == "env"
    assert parsed.segments == ("MY_KEY",)
    assert parsed.field == "field"
    with pytest.raises(ValueError, match="provider authority"):
        parse_secret_reference(reference("secret:///missing"))

    resolver = EnvironmentSecretResolver({"TOKEN": "secret-value"})
    assert resolver.resolve(reference("secret://env/TOKEN")) == "secret-value"
    with pytest.raises(ValueError, match="environment secret URI"):
        resolver.resolve(reference("secret://aws/TOKEN"))
    with pytest.raises(KeyError, match="not configured"):
        resolver.resolve(reference("secret://env/MISSING"))


def test_aws_secret_resolver_text_binary_and_fields() -> None:
    class Client:
        def __init__(self, values: list[dict[str, Any]]) -> None:
            self.values = values

        def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["SecretId"] == "service/key"
            return self.values.pop(0)

    resolver = AwsSecretsManagerResolver(
        Client(
            [
                {"SecretString": '{"key":"value","number":2}'},
                {"SecretBinary": b"binary"},
                {},
            ]
        )
    )
    assert resolver.resolve(reference("secret://aws/service/key#key")) == "value"
    assert resolver.resolve(reference("secret://aws/service/key")) == "binary"
    with pytest.raises(ValueError, match="no secret value"):
        resolver.resolve(reference("secret://aws/service/key"))
    with pytest.raises(ValueError, match="AWS secret URI"):
        resolver.resolve(reference("secret://env/key"))

    bad = AwsSecretsManagerResolver(Client([{"SecretString": '{"key":1}'}]))
    assert bad.resolve(reference("secret://aws/service/key#key")) == "1"
    missing = AwsSecretsManagerResolver(Client([{"SecretString": "{}"}]))
    with pytest.raises(KeyError, match="JSON field"):
        missing.resolve(reference("secret://aws/service/key#key"))


def test_azure_gcp_and_vault_resolvers() -> None:
    class AzureClient:
        def __init__(self, value: Any) -> None:
            self.value = value

        def get_secret(self, name: str, version: str | None) -> Any:
            assert name == "api-key"
            assert version in {None, "v1"}
            return SimpleNamespace(value=self.value)

    azure = AzureKeyVaultResolver({"vault.example": AzureClient("azure-secret")})
    assert azure.resolve(reference("secret://azure/vault.example/api-key/v1")) == "azure-secret"
    bad_azure = AzureKeyVaultResolver({"vault.example": AzureClient(None)})
    with pytest.raises(ValueError, match="no string value"):
        bad_azure.resolve(reference("secret://azure/vault.example/api-key"))
    with pytest.raises(ValueError, match="Azure secret URI"):
        azure.resolve(reference("secret://azure/only-one"))

    class GcpClient:
        def __init__(self, data: Any) -> None:
            self.data = data

        def access_secret_version(self, *, request: dict[str, str]) -> Any:
            assert request["name"] == "projects/project/secrets/key/versions/latest"
            return SimpleNamespace(payload=SimpleNamespace(data=self.data))

    assert (
        GoogleSecretManagerResolver(GcpClient(b"gcp")).resolve(
            reference("secret://gcp/project/key")
        )
        == "gcp"
    )
    assert (
        GoogleSecretManagerResolver(GcpClient("text")).resolve(
            reference("secret://gcp/project/key")
        )
        == "text"
    )
    with pytest.raises(ValueError, match="GCP secret URI"):
        GoogleSecretManagerResolver(GcpClient(b"x")).resolve(reference("secret://gcp/project"))

    class Kv:
        def read_secret_version(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {"path": "service/key", "mount_point": "models"}
            return {"data": {"data": {"token": {"nested": True}}}}

    vault_client = SimpleNamespace(secrets=SimpleNamespace(kv=SimpleNamespace(v2=Kv())))
    vault = VaultSecretResolver(vault_client, mount_point="models")
    assert vault.resolve(reference("secret://vault/service/key#token")) == '{"nested":true}'
    with pytest.raises(KeyError, match="not available"):
        vault.resolve(reference("secret://vault/service/key#missing"))
    with pytest.raises(ValueError, match="Vault secret URI"):
        vault.resolve(reference("secret://vault/service/key"))


def test_composite_secret_resolver() -> None:
    env = EnvironmentSecretResolver({"TOKEN": "value"})
    resolver = CompositeSecretResolver({"ENV": env})
    assert resolver.resolve(reference("secret://env/TOKEN")) == "value"
    with pytest.raises(KeyError, match="configured"):
        resolver.resolve(reference("secret://aws/key"))
