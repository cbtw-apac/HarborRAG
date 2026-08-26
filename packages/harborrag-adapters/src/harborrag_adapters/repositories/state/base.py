from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle
from harborrag_core.schemas.ids import WorkflowId
from harborrag_core.schemas.state import CheckpointRecord, LeaseRecord, WorkflowState
from harborrag_core.storage import StorageOperationContext


class HarborStateStore(ABC):
    """Defines optimistic workflow-state persistence independent of backend lifecycle."""

    @abstractmethod
    async def create(
        self,
        state: WorkflowState,
        *,
        context: StorageOperationContext,
    ) -> WorkflowState:
        """Create a new workflow state record."""

    @abstractmethod
    async def get(
        self,
        workflow_id: WorkflowId,
        *,
        context: StorageOperationContext,
    ) -> WorkflowState | None:
        """Load the latest workflow state."""

    @abstractmethod
    async def save(
        self,
        state: WorkflowState,
        *,
        expected_version: int,
        context: StorageOperationContext,
    ) -> WorkflowState:
        """Persist a workflow state transition with version checking."""


class HarborCheckpointStore(ABC):
    """Defines resumable workflow checkpoint persistence."""

    @abstractmethod
    async def load_latest(
        self,
        workflow_id: WorkflowId,
        *,
        context: StorageOperationContext,
    ) -> CheckpointRecord | None:
        """Load the latest checkpoint for a workflow."""

    @abstractmethod
    async def save(
        self,
        checkpoint: CheckpointRecord,
        *,
        expected_version: int | None,
        context: StorageOperationContext,
    ) -> CheckpointRecord:
        """Save a checkpoint with optimistic version validation."""


class HarborLeaseStore(ABC):
    """Defines expiring worker leases with monotonic fencing tokens."""

    @abstractmethod
    async def acquire(
        self,
        resource: str,
        owner_token: str,
        lease_duration: timedelta,
        *,
        context: StorageOperationContext,
    ) -> LeaseRecord | None:
        """Acquire an available resource lease."""

    @abstractmethod
    async def renew(
        self,
        lease: LeaseRecord,
        lease_duration: timedelta,
        *,
        context: StorageOperationContext,
    ) -> LeaseRecord:
        """Renew a lease held by the current owner."""

    @abstractmethod
    async def release(
        self,
        lease: LeaseRecord,
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Expire a lease held by the current owner."""


class HarborStateBackend(RepositoryLifecycle):
    """Owns one state backend lifecycle and its cohesive state products."""

    state: HarborStateStore
    checkpoints: HarborCheckpointStore
    leases: HarborLeaseStore
