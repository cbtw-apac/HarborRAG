"""`harborrag init` scaffolds a project non-interactively and refuses to clobber one."""

from __future__ import annotations

from pathlib import Path

from harborrag_app.cli import main as cli


def test_init_with_yes_writes_a_project_and_prints_next_steps(tmp_path: Path, capsys) -> None:
    target = tmp_path / "my-harbor"

    code = cli.main(["init", str(target), "--yes", "--api-key", "sk-test"])

    assert code == 0
    assert (target / "harborrag.yaml").is_file()
    assert (target / "config" / "models.yaml").is_file()
    assert (target / ".harborrag").is_dir()
    out = capsys.readouterr().out
    assert "docker compose up -d" in out
    assert "harborrag ingest run workspace" in out
    assert "OPENAI_API_KEY=sk-test" in (target / ".env").read_text()


def test_init_defaults_source_to_docs_when_present(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)

    assert cli.main(["init", "--yes"]) == 0

    assert "LOCAL_SOURCE_PATH=./docs" in (tmp_path / ".env").read_text()


def test_init_refuses_an_existing_project_without_force(tmp_path: Path, capsys) -> None:
    assert cli.main(["init", str(tmp_path), "--yes"]) == 0

    assert cli.main(["init", str(tmp_path), "--yes"]) == 1
    assert "--force" in capsys.readouterr().err


def test_init_force_keeps_the_existing_dotenv(tmp_path: Path, capsys) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--api-key", "first"]) == 0

    assert cli.main(["init", str(tmp_path), "--yes", "--force", "--api-key", "second"]) == 0

    assert "first" in (tmp_path / ".env").read_text()
    assert "second" in (tmp_path / ".env.new").read_text()
    assert ".env.new" in capsys.readouterr().out


def test_init_rejects_an_unknown_provider(tmp_path: Path) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--provider", "nope"]) == 2


def test_init_requires_api_base_for_gateway_providers(tmp_path: Path, capsys) -> None:
    code = cli.main(["init", str(tmp_path), "--yes", "--provider", "openai-compatible"])

    assert code == 1
    assert "--api-base" in capsys.readouterr().err


def test_init_does_not_activate_an_enclosing_project(tmp_path: Path, monkeypatch) -> None:
    """A nested init must resolve DIR from the caller's CWD, not chdir to the parent."""

    assert cli.main(["init", str(tmp_path), "--yes"]) == 0
    inner = tmp_path / "inner"
    inner.mkdir()
    monkeypatch.chdir(inner)

    assert cli.main(["init", "child", "--yes"]) == 0

    assert (inner / "child" / "harborrag.yaml").is_file()


def test_init_creates_the_source_folder_with_a_sample_document(tmp_path: Path, capsys) -> None:
    """An empty folder must still give the first `ingest run` something to ingest."""

    assert cli.main(["init", str(tmp_path), "--yes"]) == 0

    assert "LOCAL_SOURCE_PATH=./docs" in (tmp_path / ".env").read_text()
    sample = tmp_path / "docs" / "README.md"
    assert sample.is_file() and "HarborRAG" in sample.read_text()
    assert "docs/" in capsys.readouterr().out


def test_init_leaves_an_existing_source_folder_alone(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("mine\n")

    assert cli.main(["init", str(tmp_path), "--yes", "--source", "./notes"]) == 0

    assert sorted(p.name for p in (tmp_path / "notes").iterdir()) == ["a.md"]


def _busy_defaults_only(ports):
    """Fake probe: every default port (offset 0) is taken, every other port is free."""

    return [(port, service) for port, service in ports if port < 10000]


def test_init_moves_the_stack_to_a_free_port_offset_when_defaults_are_busy(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from harborrag_app.cli.commands import init_support

    monkeypatch.setattr(init_support, "_busy_ports", _busy_defaults_only)

    assert cli.main(["init", str(tmp_path), "--yes"]) == 0

    out = capsys.readouterr().out
    assert "offset 10000" in out and "16333" in out
    env = (tmp_path / ".env").read_text()
    assert "HARBORRAG_QDRANT_URL=http://localhost:16333" in env
    assert "HARBORRAG_FALKORDB_PORT=16379" in env
    assert "HARBORRAG_OBJECT_STORE_ENDPOINT_URL=http://localhost:19000" in env
    assert "HARBORRAG_QDRANT_PREFER_GRPC=false" in env
    compose = (tmp_path / "docker-compose.yml").read_text()
    assert '"127.0.0.1:16333:6333"' in compose and '"127.0.0.1:19001:9001"' in compose


def test_init_keeps_default_ports_when_they_are_free(tmp_path: Path, monkeypatch, capsys) -> None:
    from harborrag_app.cli.commands import init_support

    monkeypatch.setattr(init_support, "_busy_ports", lambda ports: [])

    assert cli.main(["init", str(tmp_path), "--yes"]) == 0

    env = (tmp_path / ".env").read_text()
    assert "HARBORRAG_QDRANT_URL=http://localhost:6333" in env
    assert "HARBORRAG_QDRANT_PREFER_GRPC" not in env
    assert "offset" not in capsys.readouterr().out


def test_init_honours_an_explicit_ports_offset(tmp_path: Path, monkeypatch) -> None:
    from harborrag_app.cli.commands import init_support

    monkeypatch.setattr(init_support, "_busy_ports", lambda ports: [])

    assert cli.main(["init", str(tmp_path), "--yes", "--ports-offset", "20000"]) == 0

    env = (tmp_path / ".env").read_text()
    assert "HARBORRAG_OBJECT_STORE_ENDPOINT_URL=http://localhost:29000" in env
    assert '"127.0.0.1:26379:6379"' in (tmp_path / "docker-compose.yml").read_text()


def test_init_connectors_flag_scaffolds_the_selected_sources(tmp_path: Path, capsys) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--connectors", "github, jira"]) == 0

    out = capsys.readouterr().out
    connectors = (tmp_path / "config" / "connectors.yaml").read_text()
    assert "  github:" in connectors and "  jira:" in connectors and "workspace" not in connectors
    assert "harborrag ingest run github" in out and "harborrag ingest run jira" in out
    assert "GITHUB_TOKEN" in out  # blank credential called out in the next steps
    assert not (tmp_path / "docs").exists()  # no local source, no sample folder


def test_init_rejects_an_unknown_connector(tmp_path: Path) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--connectors", "sharepoint"]) == 2


def _interactive(monkeypatch, *, confirm: bool) -> list[str]:
    """Pretend to be a TTY: prompts return their defaults, the overwrite question answers `confirm`."""

    from harborrag_app.cli.commands import init as init_command

    questions: list[str] = []
    monkeypatch.setattr(init_command.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(init_command.Prompt, "ask", lambda label, *a, default="", **k: default)

    def fake_confirm(question, *a, default=False, **k):
        questions.append(str(question))
        return confirm

    monkeypatch.setattr(init_command.Confirm, "ask", fake_confirm)
    return questions


def test_init_offers_to_overwrite_an_existing_project_interactively(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--api-key", "first"]) == 0
    (tmp_path / "config" / "models.yaml").write_text("stale: true\n")
    questions = _interactive(monkeypatch, confirm=True)

    assert cli.main(["init", str(tmp_path)]) == 0

    assert len(questions) == 1 and "verwrite" in questions[0]
    assert "stale" not in (tmp_path / "config" / "models.yaml").read_text()
    assert "first" in (tmp_path / ".env").read_text()  # .env is still never overwritten
    assert (tmp_path / ".env.new").exists()


def test_init_declining_the_overwrite_leaves_everything_untouched(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    assert cli.main(["init", str(tmp_path), "--yes"]) == 0
    (tmp_path / "config" / "models.yaml").write_text("stale: true\n")
    _interactive(monkeypatch, confirm=False)

    assert cli.main(["init", str(tmp_path)]) == 1

    assert (tmp_path / "config" / "models.yaml").read_text() == "stale: true\n"
    assert not (tmp_path / ".env.new").exists()
    assert "untouched" in capsys.readouterr().err
