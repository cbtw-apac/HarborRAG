"""Contract suite and in-memory fake for source connectors."""

from harborrag_core.sources.testing.contract import (
    CONTRACT_CHECKS,
    CheckOutcome,
    ContractCheck,
    ContractScenario,
    Run,
    collect,
)
from harborrag_core.sources.testing.credentials import InMemoryCredentialStore
from harborrag_core.sources.testing.fake import (
    FAKE_CHILD,
    FAKE_PARENT,
    FakeCheckpoint,
    FakeConfig,
    FakeItem,
    FakeSourceConnector,
)

__all__ = [
    "CONTRACT_CHECKS",
    "FAKE_CHILD",
    "FAKE_PARENT",
    "CheckOutcome",
    "ContractCheck",
    "ContractScenario",
    "FakeCheckpoint",
    "FakeConfig",
    "FakeItem",
    "FakeSourceConnector",
    "InMemoryCredentialStore",
    "Run",
    "collect",
]
