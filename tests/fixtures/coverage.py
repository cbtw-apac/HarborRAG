"""Fixture plugin for sample_coverage_data."""

import json

import pytest


@pytest.fixture
def sample_coverage_data(tmp_path):
    """Create sample coverage data directory with mock fixtures."""
    coverage_dir = tmp_path / "coverage-artifacts"
    coverage_dir.mkdir()

    # Always use mock data for predictable testing

    # Create loader coverage data
    loader_dir = coverage_dir / "htmlcov-loader"
    loader_dir.mkdir()
    (loader_dir / "index.html").write_text("<html><body>Loader Coverage Report</body></html>")
    mock_loader_status = {
        "note": "Mock loader coverage data for testing",
        "format": 5,
        "version": "7.8.2",
        "globals": "mock_hash",
        "files": {
            "mock_loader_file_py": {
                "hash": "mock_hash",
                "index": {
                    "url": "mock_loader_file_py.html",
                    "file": "src/qdrant_loader/mock_file.py",
                    "description": "",
                    "nums": {
                        "precision": 0,
                        "n_files": 1,
                        "n_statements": 100,
                        "n_excluded": 0,
                        "n_missing": 15,
                        "n_branches": 0,
                        "n_partial_branches": 0,
                        "n_missing_branches": 0,
                    },
                },
            }
        },
    }
    (loader_dir / "status.json").write_text(json.dumps(mock_loader_status, indent=2))

    # Create MCP coverage data
    mcp_dir = coverage_dir / "htmlcov-mcp"
    mcp_dir.mkdir()
    (mcp_dir / "index.html").write_text("<html><body>MCP Coverage Report</body></html>")
    mock_mcp_status = {
        "note": "Mock MCP coverage data for testing",
        "format": 5,
        "version": "7.8.2",
        "globals": "mock_hash",
        "files": {
            "mock_mcp_file_py": {
                "hash": "mock_hash",
                "index": {
                    "url": "mock_mcp_file_py.html",
                    "file": "src/mcp_server/mock_file.py",
                    "description": "",
                    "nums": {
                        "precision": 0,
                        "n_files": 1,
                        "n_statements": 50,
                        "n_excluded": 0,
                        "n_missing": 4,
                        "n_branches": 0,
                        "n_partial_branches": 0,
                        "n_missing_branches": 0,
                    },
                },
            }
        },
    }
    (mcp_dir / "status.json").write_text(json.dumps(mock_mcp_status, indent=2))

    # Create website coverage data
    website_dir = coverage_dir / "htmlcov-website"
    website_dir.mkdir()
    (website_dir / "index.html").write_text("<html><body>Website Coverage Report</body></html>")
    mock_website_status = {
        "note": "Mock website coverage data for testing",
        "format": 5,
        "version": "7.8.2",
        "globals": "mock_hash",
        "files": {
            "mock_website_file_py": {
                "hash": "mock_hash",
                "index": {
                    "url": "mock_website_file_py.html",
                    "file": "website/build.py",
                    "description": "",
                    "nums": {
                        "precision": 0,
                        "n_files": 1,
                        "n_statements": 75,
                        "n_excluded": 0,
                        "n_missing": 8,
                        "n_branches": 0,
                        "n_partial_branches": 0,
                        "n_missing_branches": 0,
                    },
                },
            }
        },
    }
    (website_dir / "status.json").write_text(json.dumps(mock_website_status, indent=2))

    return coverage_dir
