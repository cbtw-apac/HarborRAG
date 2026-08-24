"""Golden retrieval expectations: what the corpus topology guarantees the graph answers.

``case_types.py`` holds one dataclass per query shape, each scoring its own answer;
``cases.py`` holds the cases. Consumers import from the package, never the submodules,
so adding a case type does not move anyone's import.
"""

from .case_types import PathCase, StalenessCase, SubgraphCase, TripletCase
from .cases import PATH_CASES, STALENESS_CASES, SUBGRAPH_CASES, TRIPLET_CASES

__all__ = [
    "PATH_CASES",
    "STALENESS_CASES",
    "SUBGRAPH_CASES",
    "TRIPLET_CASES",
    "PathCase",
    "StalenessCase",
    "SubgraphCase",
    "TripletCase",
]
