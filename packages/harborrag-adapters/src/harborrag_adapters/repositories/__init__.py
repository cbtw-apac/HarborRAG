from harborrag_adapters.repositories.cache import (
    BaseCacheRepository,
    MockCacheRepository,
)
from harborrag_adapters.repositories.database import (
    BaseDatabaseRepository,
    MockDatabaseRepository,
)
from harborrag_adapters.repositories.object_store import (
    BaseObjectRepository,
    MockObjectRepository,
)
from harborrag_adapters.repositories.vector import (
    BaseVectorRepository,
    MockVectorRepository,
)

__all__ = [
    "BaseCacheRepository",
    "BaseDatabaseRepository",
    "BaseObjectRepository",
    "BaseVectorRepository",
    "MockCacheRepository",
    "MockDatabaseRepository",
    "MockObjectRepository",
    "MockVectorRepository",
]

try:
    # repositories.graph depends on harborrag_core.domain.graph.GraphHint, which
    # doesn't exist yet. Isolate the failure here so it doesn't take down the
    # other (working) repository submodules re-exported by this package.
    from harborrag_adapters.repositories.graph import (
        BaseGraphRepository,
        MockGraphRepository,
    )
except ImportError:
    pass
else:  # pragma: no cover - unreachable until harborrag_core.domain.graph exists
    __all__ += ["BaseGraphRepository", "MockGraphRepository"]
