SHELL := /bin/bash
PYTHON ?= python
PYTEST ?= pytest
PACKAGE ?=

.PHONY: help bootstrap install-dev test test-package coverage coverage-html openapi lint complexity file-length import-boundaries format typecheck compile doctor deps-check provider-matrix clean

help:
	@echo "HarborRAG development targets"
	@echo ""
	@echo "Setup:"
	@echo "  make bootstrap        Install workspace in editable mode with dev tools"
	@echo "  make install-dev      Alias for bootstrap"
	@echo ""
	@echo "Quality:"
	@echo "  make test             Run all root and package-local tests"
	@echo "  make test-package PACKAGE=harborrag-core"
	@echo "  make coverage         Run tests with 90% coverage gate"
	@echo "  make openapi          Export the OpenAPI contract to openapi.json"
	@echo "  make lint             Run Ruff lint checks"
	@echo "  make complexity       Enforce the Ruff complexity ratchet"
	@echo "  make file-length      Require every Python file to stay under 350 lines"
	@echo "  make import-boundaries Run import-linter architecture contracts"
	@echo "  make format           Format and fix imports with Ruff"
	@echo "  make typecheck        Run mypy across packages"
	@echo "  make compile          Compile package and script Python files"
	@echo ""
	@echo "Diagnostics:"
	@echo "  make doctor           Run CLI doctor as JSON"
	@echo "  make deps-check       Check package dependency direction"
	@echo "  make provider-matrix  Print provider/repository TODO matrix"
	@echo "  make clean            Remove Python build/test caches"

bootstrap:
	$(PYTHON) -m pip install -e packages/harborrag-core
	$(PYTHON) -m pip install -e "packages/harborrag-adapters[control-plane]"
	$(PYTHON) -m pip install -e packages/harborrag-engine
	$(PYTHON) -m pip install -e "packages/harborrag-runtime[production]"
	$(PYTHON) -m pip install -e "packages/harborrag-app[api]"
	$(PYTHON) -m pip install -e packages/harborrag-mcp-server
	$(PYTHON) -m pip install -e packages/harborrag
	$(PYTHON) -m pip install -e ".[dev]"

install-dev: bootstrap

test:
	$(PYTEST)

test-package:
	@if [[ -z "$(PACKAGE)" ]]; then \
		echo "Usage: make test-package PACKAGE=harborrag-core"; \
		exit 2; \
	fi
	$(PYTEST) packages/$(PACKAGE)/tests

coverage:
	$(PYTEST) -n 4 --cov --cov-report=term-missing

openapi:
	$(PYTHON) -m harborrag_app.api.export_openapi > openapi.json
	@echo "openapi.json written"

coverage-html:
	$(PYTEST) --cov --cov-report=term-missing --cov-report=html

lint:
	ruff format --check packages tests scripts
	ruff check --ignore C901,PLR0913 .

complexity:
	$(PYTHON) scripts/check_ruff_complexity.py

file-length:
	$(PYTHON) scripts/check_python_file_length.py

import-boundaries:
	lint-imports

format:
	ruff format packages tests scripts
	ruff check --fix --ignore C901,PLR0913 packages tests scripts

typecheck:
	mypy packages

compile:
	$(PYTHON) -m compileall -q packages scripts

doctor:
	$(PYTHON) -m harborrag_app.cli.main doctor --json

deps-check:
	$(PYTHON) scripts/check_dependency_direction.py

provider-matrix:
	$(PYTHON) scripts/generate_provider_matrix.py

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache -o -name htmlcov \) -prune -exec rm -rf {} +
	find . -type f \( -name "*.pyc" -o -name ".coverage" -o -name "coverage.xml" \) -delete
