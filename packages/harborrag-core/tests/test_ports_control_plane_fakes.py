"""Control-plane repository fakes behave like their persistence ports."""

import pytest
from core_test_fixtures import FakeProjectRepository

from harborrag_core.domain.project import Project


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fake_project_repository_crud_roundtrip() -> None:
    """Create/get/list/update/delete round-trip on the dict-backed fake."""
    repo = FakeProjectRepository()
    project = Project(id="p1", name="Docs", collection="docs_main")
    await repo.create(project)
    assert await repo.get("p1") == project
    assert await repo.list() == [project]
    project.name = "Docs v2"
    await repo.update(project)
    fetched = await repo.get("p1")
    assert fetched is not None and fetched.name == "Docs v2"
    await repo.delete("p1")
    assert await repo.get("p1") is None
