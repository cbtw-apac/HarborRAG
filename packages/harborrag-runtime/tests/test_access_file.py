"""The beta access file: loading, validation, entitlements, watching, and its settings guard."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from harborrag_runtime.config import (
    ConnectorCatalog,
    ConnectorConfigurationError,
    load_connector_catalog,
)
from harborrag_runtime.config import access_file as access_file_module
from harborrag_runtime.config.access_file import (
    AccessFile,
    AccessFileWatcher,
    AccessMember,
    AccessProject,
    load_access_file,
)
from harborrag_runtime.config.settings import RuntimeSettings

_ACCESS_LOGGER = "harborrag.runtime.config.access_file"

_CATALOG = """
version: 1
connectors:
  confluence-eng:
    provider: local
  jira-cpm:
    provider: local
    tenants: [acme]
"""

_DOCUMENTED_EXAMPLE = """
version: 1
tenants:
  acme:
    projects:
      beta-all:
        datasources: [confluence-eng, jira-cpm]
        members:
          - principal: "6d2f…e91a"
            label: alice@acme.com        # optional, for people only, never matched and never logged
            datasources: [confluence-eng] # optional narrowing within the project
"""


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    path = tmp_path / "connectors.yaml"
    path.write_text(dedent(_CATALOG), encoding="utf-8")
    return path


@pytest.fixture
def catalog(catalog_path: Path) -> ConnectorCatalog:
    return load_connector_catalog(catalog_path)


def _document() -> dict[str, Any]:
    """A valid access file as plain data, for tests to break one thing at a time."""

    return {
        "version": 1,
        "tenants": {
            "acme": {
                "projects": {
                    "beta-all": {
                        "datasources": ["confluence-eng", "jira-cpm"],
                        "members": [
                            {
                                "principal": "p-aaaa",
                                "label": "alice@acme.com",
                                "datasources": ["confluence-eng"],
                            },
                            {"principal": "p-whole"},
                        ],
                    }
                }
            }
        },
    }


def _project(document: dict[str, Any]) -> dict[str, Any]:
    project: dict[str, Any] = document["tenants"]["acme"]["projects"]["beta-all"]
    return project


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _load(tmp_path: Path, document: dict[str, Any], catalog: ConnectorCatalog) -> AccessFile:
    return load_access_file(_write(tmp_path / "access.yaml", document), catalog=catalog)


def test_loads_the_documented_example(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    path = tmp_path / "access.yaml"
    path.write_text(dedent(_DOCUMENTED_EXAMPLE), encoding="utf-8")

    loaded = load_access_file(path, catalog=catalog)

    assert list(loaded.tenants) == ["acme"]
    assert list(loaded.tenants["acme"]) == ["beta-all"]
    project = loaded.tenants["acme"]["beta-all"]
    assert project == AccessProject(
        datasources=frozenset({"confluence-eng", "jira-cpm"}),
        members=(
            AccessMember(
                principal="6d2f…e91a",
                label="alice@acme.com",
                datasources=frozenset({"confluence-eng"}),
            ),
        ),
    )
    assert loaded.entitled_connections("acme", "6d2f…e91a") == frozenset({"confluence-eng"})


def test_entitled_connections_follow_the_project_rule() -> None:
    both = frozenset({"confluence-eng", "jira-cpm"})
    loaded = AccessFile(
        {
            "acme": {
                "alpha": AccessProject(
                    frozenset({"confluence-eng"}),
                    (
                        AccessMember("alice"),
                        AccessMember("bob", datasources=frozenset({"confluence-eng"})),
                        # Narrowed past what its project owns: the project bounds it.
                        AccessMember("dan", datasources=both),
                    ),
                ),
                "beta": AccessProject(
                    frozenset({"jira-cpm"}),
                    (
                        AccessMember("alice"),
                        AccessMember("carol", datasources=frozenset()),
                    ),
                ),
            },
            "globex": {
                "gamma": AccessProject(
                    frozenset({"confluence-eng"}),
                    (AccessMember("alice", datasources=frozenset({"confluence-eng"})),),
                ),
            },
        }
    )

    assert loaded.entitled_connections("acme", "alice") == both
    assert loaded.entitled_connections("acme", "bob") == frozenset({"confluence-eng"})
    assert loaded.entitled_connections("acme", "dan") == frozenset({"confluence-eng"})
    assert loaded.entitled_connections("acme", "carol") == frozenset()
    assert loaded.entitled_connections("acme", "nobody") == frozenset()
    # A tenant sees only its own projects.
    assert loaded.entitled_connections("globex", "alice") == frozenset({"confluence-eng"})
    assert loaded.entitled_connections("globex", "bob") == frozenset()
    # A tenant absent from the file is not the same as a tenant with no entitlement.
    assert loaded.entitled_connections("initech", "alice") is None


def test_the_loaded_file_cannot_be_edited_through_its_views(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    loaded = _load(tmp_path, _document(), catalog)

    with pytest.raises(TypeError):
        loaded.tenants["initech"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        loaded.tenants["acme"]["extra"] = loaded.tenants["acme"]["beta-all"]  # type: ignore[index]


@pytest.mark.parametrize("line", ["version: 2", "version: '1'", "version: true", ""])
def test_refuses_version_other_than_1(tmp_path: Path, catalog: ConnectorCatalog, line: str) -> None:
    path = tmp_path / "access.yaml"
    path.write_text(f"{line}\ntenants: {{}}\n", encoding="utf-8")

    with pytest.raises(ConnectorConfigurationError, match="version"):
        load_access_file(path, catalog=catalog)


@pytest.mark.parametrize(
    ("level", "expected_label"),
    [
        ("root", "access configuration root"),
        ("tenant", "tenants.acme"),
        ("project", "tenants.acme.projects.beta-all"),
        ("member", "members[0]"),
    ],
)
def test_refuses_unknown_keys_at_every_level(
    tmp_path: Path, catalog: ConnectorCatalog, level: str, expected_label: str
) -> None:
    document = _document()
    targets: dict[str, dict[str, Any]] = {
        "root": document,
        "tenant": document["tenants"]["acme"],
        "project": _project(document),
        "member": _project(document)["members"][0],
    }
    targets[level]["surprise"] = 1

    with pytest.raises(ConnectorConfigurationError, match="surprise") as caught:
        _load(tmp_path, document, catalog)

    assert expected_label in str(caught.value)


def test_refuses_unknown_connection(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    document = _document()
    _project(document)["datasources"].append("no-such-connection")

    with pytest.raises(ConnectorConfigurationError, match="no-such-connection") as caught:
        _load(tmp_path, document, catalog)

    assert "acme" in str(caught.value)


def test_refuses_connection_not_allowed_for_the_tenant(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    document = _document()
    document["tenants"]["globex"] = document["tenants"].pop("acme")

    with pytest.raises(ConnectorConfigurationError) as caught:
        _load(tmp_path, document, catalog)

    message = str(caught.value)
    assert "jira-cpm" in message
    assert "globex" in message
    assert "confluence-eng" not in message
    assert catalog.get_for_tenant("confluence-eng", "globex").name == "confluence-eng"


def test_refuses_member_datasources_outside_the_project(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    document = _document()
    _project(document)["datasources"] = ["confluence-eng"]
    _project(document)["members"][0]["datasources"] = ["confluence-eng", "jira-cpm"]

    with pytest.raises(ConnectorConfigurationError, match="jira-cpm") as caught:
        _load(tmp_path, document, catalog)

    assert "members[0]" in str(caught.value)


@pytest.mark.parametrize("bare", ["confluence-eng", 5, {"confluence-eng": 1}])
def test_refuses_datasources_that_are_not_a_list_of_connection_ids(
    tmp_path: Path, catalog: ConnectorCatalog, bare: object
) -> None:
    project_document = _document()
    _project(project_document)["datasources"] = bare
    member_document = _document()
    _project(member_document)["members"][0]["datasources"] = bare

    for document in (project_document, member_document):
        with pytest.raises(ConnectorConfigurationError, match="datasources"):
            _load(tmp_path, document, catalog)


@pytest.mark.parametrize("bad_item", ["", "  ", " confluence-eng", 5, None])
def test_refuses_blank_or_non_string_connection_ids(
    tmp_path: Path, catalog: ConnectorCatalog, bad_item: object
) -> None:
    document = _document()
    _project(document)["datasources"].append(bad_item)

    with pytest.raises(ConnectorConfigurationError, match="datasources"):
        _load(tmp_path, document, catalog)


def test_refuses_a_repeated_connection_id(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    document = _document()
    _project(document)["datasources"] = ["confluence-eng", "confluence-eng"]

    with pytest.raises(ConnectorConfigurationError, match="confluence-eng"):
        _load(tmp_path, document, catalog)


@pytest.mark.parametrize("principal", ["", "   ", " padded", 12345, None])
def test_refuses_blank_or_duplicate_principal_in_one_project(
    tmp_path: Path, catalog: ConnectorCatalog, principal: object
) -> None:
    document = _document()
    _project(document)["members"][1]["principal"] = principal

    with pytest.raises(ConnectorConfigurationError, match="principal") as caught:
        _load(tmp_path, document, catalog)

    assert "members[1]" in str(caught.value)


def test_refuses_a_principal_listed_twice_in_one_project(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    document = _document()
    _project(document)["members"][1]["principal"] = "p-aaaa"

    with pytest.raises(ConnectorConfigurationError, match="more than once") as caught:
        _load(tmp_path, document, catalog)

    message = str(caught.value)
    assert "members[0]" in message
    assert "members[1]" in message
    assert "p-aaaa" not in message


def test_the_same_principal_may_join_two_projects(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    document = _document()
    document["tenants"]["acme"]["projects"]["second"] = copy.deepcopy(_project(document))

    loaded = _load(tmp_path, document, catalog)

    assert loaded.entitled_connections("acme", "p-aaaa") == frozenset({"confluence-eng"})


def test_refuses_principal_longer_than_255(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    ok = _document()
    _project(ok)["members"][1]["principal"] = "x" * 255
    assert _load(tmp_path, ok, catalog).entitled_connections("acme", "x" * 255) == frozenset(
        {"confluence-eng", "jira-cpm"}
    )

    too_long = _document()
    _project(too_long)["members"][1]["principal"] = "y" * 256
    with pytest.raises(ConnectorConfigurationError, match="255") as caught:
        _load(tmp_path, too_long, catalog)

    assert "y" * 20 not in str(caught.value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("tenants",), []),
        (("tenants",), None),
        (("tenants", "acme", "projects"), []),
        (("tenants", "acme"), {"projects": None}),
        (("tenants", "acme", "projects", "beta-all", "members"), {"principal": "x"}),
        (("tenants", "acme", "projects", "beta-all", "members"), None),
        (("tenants", "acme", "projects", "beta-all", "members"), ["alice"]),
        (("tenants", "acme", "projects", "beta-all", "datasources"), None),
    ],
)
def test_refuses_values_of_the_wrong_shape(
    tmp_path: Path, catalog: ConnectorCatalog, path: tuple[str, ...], value: object
) -> None:
    document = _document()
    parent: Any = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value

    with pytest.raises(ConnectorConfigurationError):
        _load(tmp_path, document, catalog)


@pytest.mark.parametrize("missing", ["datasources", "members"])
def test_a_project_states_its_datasources_and_its_members(
    tmp_path: Path, catalog: ConnectorCatalog, missing: str
) -> None:
    document = _document()
    del _project(document)[missing]

    with pytest.raises(ConnectorConfigurationError, match=missing):
        _load(tmp_path, document, catalog)


def test_a_project_with_no_members_entitles_nobody(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    document = _document()
    _project(document)["members"] = []

    loaded = _load(tmp_path, document, catalog)

    assert loaded.entitled_connections("acme", "p-aaaa") == frozenset()


def test_label_is_optional_and_never_in_errors(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    label = "secret.person@acme.com"
    principal = "principal-id-0001"

    plain = _document()
    plain_member = _project(plain)["members"][1]
    assert "label" not in plain_member
    loaded = _load(tmp_path, plain, catalog)
    assert loaded.tenants["acme"]["beta-all"].members[1].label is None

    labelled = _document()
    _project(labelled)["members"][0].update({"label": label, "principal": principal})
    loaded = _load(tmp_path, labelled, catalog)
    assert loaded.tenants["acme"]["beta-all"].members[0].label == label
    assert label not in repr(loaded)
    assert principal not in repr(loaded)

    def broken(mutate: Any) -> dict[str, Any]:
        document = copy.deepcopy(labelled)
        mutate(_project(document)["members"][0])
        return document

    broken_documents = [
        broken(lambda member: member.update({"datasources": ["jira-cpm", "no-such"]})),
        broken(lambda member: member.update({"surprise": 1})),
        broken(lambda member: member.update({"principal": ""})),
        broken(lambda member: member.update({"principal": "z" * 300})),
        broken(lambda member: member.update({"label": ""})),
        broken(lambda member: member.update({"label": 7})),
        broken(lambda member: member.update({"label": ["a", label]})),
        broken(lambda member: member.update({"principal": None})),
    ]
    twice = copy.deepcopy(labelled)
    _project(twice)["members"].append({"principal": principal, "label": label})
    broken_documents.append(twice)

    for document in broken_documents:
        with pytest.raises(ConnectorConfigurationError) as caught:
            _load(tmp_path, document, catalog)
        assert label not in str(caught.value)
        assert principal not in str(caught.value)


def test_a_yaml_syntax_error_does_not_quote_the_file(
    tmp_path: Path, catalog: ConnectorCatalog
) -> None:
    path = tmp_path / "access.yaml"
    path.write_text(
        dedent(
            """
            version: 1
            tenants:
              acme:
                projects:
                  beta-all:
                    datasources: [confluence-eng]
                    members:
                      - principal: principal-id-0001
                        label: secret.person@acme.com: oops
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorConfigurationError, match="line") as caught:
        load_access_file(path, catalog=catalog)

    message = str(caught.value)
    assert str(path.resolve()) in message
    assert "secret.person" not in message
    assert "principal-id-0001" not in message
    assert caught.value.__cause__ is None


def test_refuses_an_empty_or_missing_file(tmp_path: Path, catalog: ConnectorCatalog) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(ConnectorConfigurationError):
        load_access_file(empty, catalog=catalog)
    with pytest.raises(ConnectorConfigurationError, match="does not exist"):
        load_access_file(tmp_path / "absent.yaml", catalog=catalog)


@pytest.fixture
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HARBORRAG_ACCESS_CONFIG_PATH",
        "HARBORRAG_MEMORY_RECALL_SCOPES",
        "HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("clean_settings_env")
def test_settings_refuse_shared_memory_with_access_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HARBORRAG_MEMORY_RECALL_SCOPES"):
        RuntimeSettings(access_config_path=tmp_path / "access.yaml")


@pytest.mark.usefixtures("clean_settings_env")
@pytest.mark.parametrize("scopes", ["user,project", "tenant,user", "project", "session,tenant"])
def test_settings_refuse_each_shared_recall_scope_with_access_file(
    tmp_path: Path, scopes: str
) -> None:
    with pytest.raises(ValidationError, match="HARBORRAG_MEMORY_RECALL_SCOPES"):
        RuntimeSettings(access_config_path=tmp_path / "access.yaml", memory_recall_scopes=scopes)


@pytest.mark.usefixtures("clean_settings_env")
def test_settings_refuse_project_memory_extraction_with_access_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HARBORRAG_MEMORY_EXTRACTION_PROJECT_SCOPE"):
        RuntimeSettings(
            access_config_path=tmp_path / "access.yaml",
            memory_recall_scopes="user,session",
            memory_extraction_project_scope=True,
        )


@pytest.mark.usefixtures("clean_settings_env")
def test_settings_accept_personal_memory_with_access_file(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        access_config_path=tmp_path / "access.yaml", memory_recall_scopes="user,session"
    )

    assert settings.access_config_path == tmp_path / "access.yaml"


@pytest.mark.usefixtures("clean_settings_env")
def test_settings_are_unchanged_without_an_access_file(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = RuntimeSettings(memory_extraction_project_scope=True)

    assert settings.access_config_path is None
    assert "project" in settings.memory_recall_scopes

    monkeypatch.setenv("HARBORRAG_ACCESS_CONFIG_PATH", "config/access.yaml")
    monkeypatch.setenv("HARBORRAG_MEMORY_RECALL_SCOPES", "user")
    assert RuntimeSettings().access_config_path == Path("config/access.yaml")


def test_watcher_reads_nothing_until_asked(tmp_path: Path, catalog_path: Path) -> None:
    watcher = AccessFileWatcher(tmp_path / "absent.yaml", catalog_path=catalog_path)

    assert watcher.current() is None
    assert watcher.listed_tenants("anyone") == frozenset()


def test_watcher_reloads_only_when_the_file_changes(
    tmp_path: Path, catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "access.yaml", _document())
    loads: list[Path] = []
    real_load = access_file_module.load_access_file

    def counting(file: Path, *, catalog: ConnectorCatalog) -> AccessFile:
        loads.append(file)
        return real_load(file, catalog=catalog)

    monkeypatch.setattr(access_file_module, "load_access_file", counting)
    watcher = AccessFileWatcher(path, catalog_path=catalog_path)

    first = watcher.current()

    assert first is not None
    assert watcher.current() is first
    assert watcher.current() is first
    assert len(loads) == 1


def test_watcher_reloads_on_change_and_refuses_when_missing_or_invalid(
    tmp_path: Path, catalog_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    label = "secret.person@acme.com"
    path = tmp_path / "access.yaml"
    document = _document()
    _project(document)["members"][0]["label"] = label
    _write(path, document)
    watcher = AccessFileWatcher(path, catalog_path=catalog_path)
    caplog.set_level(logging.INFO, logger=_ACCESS_LOGGER)

    def entitled(principal: str) -> frozenset[str] | None:
        current = watcher.current()
        return None if current is None else current.entitled_connections("acme", principal)

    def access_records() -> list[logging.LogRecord]:
        return [record for record in caplog.records if record.name == _ACCESS_LOGGER]

    # The first read.
    assert entitled("p-aaaa") == frozenset({"confluence-eng"})
    seen = path.stat()

    # A new size, with the modification time put back where it was.
    resized = copy.deepcopy(document)
    _project(resized)["members"][0]["datasources"] = ["confluence-eng", "jira-cpm"]
    _write(path, resized)
    os.utime(path, ns=(seen.st_atime_ns, seen.st_mtime_ns))
    assert path.stat().st_mtime_ns == seen.st_mtime_ns
    assert path.stat().st_size != seen.st_size
    assert entitled("p-aaaa") == frozenset({"confluence-eng", "jira-cpm"})
    seen = path.stat()

    # The same size and different content, seen only because the modification time moved.
    renamed = copy.deepcopy(resized)
    _project(renamed)["members"][0]["principal"] = "p-bbbb"
    _write(path, renamed)
    assert path.stat().st_size == seen.st_size
    os.utime(path, ns=(seen.st_atime_ns, seen.st_mtime_ns + 10**9))
    assert entitled("p-bbbb") == frozenset({"confluence-eng", "jira-cpm"})
    assert entitled("p-aaaa") == frozenset()
    assert access_records() == []

    # Deleted: refused, and said once.
    path.unlink()
    assert watcher.current() is None
    assert watcher.current() is None
    assert watcher.listed_tenants("p-bbbb") == frozenset()
    deleted = access_records()
    assert len(deleted) == 1
    assert deleted[0].levelno == logging.WARNING
    assert str(path) in deleted[0].getMessage()

    # Invalid: refused, and said once, without the file's contents.
    invalid = copy.deepcopy(document)
    _project(invalid)["members"][0].update({"surprise": 1, "principal": "principal-id-0001"})
    _write(path, invalid)
    assert watcher.current() is None
    assert watcher.current() is None
    assert watcher.listed_tenants("principal-id-0001") == frozenset()
    records = access_records()
    assert len(records) == 2
    record = records[1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    message = record.getMessage()
    assert str(path) in message
    assert "ConnectorConfigurationError" in message
    for secret in (label, "principal-id-0001", "p-aaaa", "surprise"):
        assert secret not in caplog.text

    # A different invalid edit is a change, so it is said again.
    _project(invalid)["members"][0]["principal"] = "principal-id-0002x"
    _write(path, invalid)
    assert watcher.current() is None
    assert len(access_records()) == 3

    # Restored: recovered.
    _write(path, document)
    assert entitled("p-aaaa") == frozenset({"confluence-eng"})
    assert len(access_records()) == 3


def test_watcher_refuses_when_the_catalog_cannot_be_read(
    tmp_path: Path, catalog_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "access.yaml", _document())
    watcher = AccessFileWatcher(path, catalog_path=tmp_path / "absent-connectors.yaml")

    assert watcher.current() is None
    records = [record for record in caplog.records if record.name == _ACCESS_LOGGER]
    assert len(records) == 1
    assert str(path) in records[0].getMessage()

    healthy = AccessFileWatcher(path, catalog_path=catalog_path)
    assert healthy.current() is not None


def test_watcher_refuses_a_file_that_raises_something_unexpected(
    tmp_path: Path,
    catalog_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path / "access.yaml", _document())

    def explode(file: Path, *, catalog: ConnectorCatalog) -> AccessFile:
        raise TypeError("principal-id-0001 label secret.person@acme.com")

    monkeypatch.setattr(access_file_module, "load_access_file", explode)
    watcher = AccessFileWatcher(path, catalog_path=catalog_path)

    assert watcher.current() is None
    assert watcher.current() is None
    records = [record for record in caplog.records if record.name == _ACCESS_LOGGER]
    assert len(records) == 1
    assert "TypeError" in records[0].getMessage()
    assert "secret.person" not in caplog.text


def test_listed_tenants(tmp_path: Path, catalog_path: Path) -> None:
    document = _document()
    document["tenants"]["globex"] = {
        "projects": {
            "gamma": {
                "datasources": ["confluence-eng"],
                "members": [
                    {"principal": "p-aaaa", "datasources": []},
                    {"principal": "p-globex"},
                ],
            }
        }
    }
    document["tenants"]["empty"] = {"projects": {}}
    path = _write(tmp_path / "access.yaml", document)
    watcher = AccessFileWatcher(path, catalog_path=catalog_path)

    assert watcher.listed_tenants("p-aaaa") == frozenset({"acme"})
    assert watcher.listed_tenants("p-whole") == frozenset({"acme"})
    assert watcher.listed_tenants("p-globex") == frozenset({"globex"})
    assert watcher.listed_tenants("nobody") == frozenset()

    path.unlink()
    assert watcher.listed_tenants("p-aaaa") == frozenset()
