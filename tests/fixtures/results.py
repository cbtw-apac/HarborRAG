"""Fixture plugin for sample_test_results."""

import json

import pytest


@pytest.fixture
def sample_test_results(temp_workspace):
    """Create sample test results for testing."""
    test_results_dir = temp_workspace / "test-results"
    test_results_dir.mkdir(exist_ok=True)  # Ensure parent directory exists

    status_data = {
        "overall_status": "success",
        "timestamp": "2025-01-31T12:00:00Z",
        "loader_status": "success",
        "mcp_status": "success",
        "website_status": "success",
        "run_id": "12345",
        "commit_sha": "abc123def456",
        "branch": "main",
    }

    (test_results_dir / "status.json").write_text(json.dumps(status_data, indent=2))

    return test_results_dir
