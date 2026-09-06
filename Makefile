.PHONY: help all test lint ruff fmt check-fmt markdownlint matrix expected upstream

UV ?= uv
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
RUFF_VERSION ?= 0.15.12
PYTEST_DEPS = --with 'pytest>=8,<10' --with 'pyyaml>=6,<7' --with 'hypothesis>=6,<7'
PY_SOURCES := scripts tests
MDLINT ?= markdownlint-cli2

all: check-fmt lint test ## Run every commit gate

test: ## Run the unit tests, property tests and workflow contracts
	$(UV_ENV) $(UV) run --no-project --python 3.13 $(PYTEST_DEPS) python -m pytest -q

lint: markdownlint ruff ## Lint Python and Markdown sources

ruff: ## Lint Python sources
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) check $(PY_SOURCES)

markdownlint: ## Lint Markdown files
	$(MDLINT) "**/*.md" "#.uv-cache" "#.uv-tools" "#.venv"

fmt: ## Format Python sources
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) format $(PY_SOURCES)
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) check --fix $(PY_SOURCES)

check-fmt: ## Verify Python formatting
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) format --check $(PY_SOURCES)

matrix: ## Print the release build matrix derived from dylint.toml
	python3 scripts/matrix.py matrix

expected: ## Print the asset names a release must publish
	python3 scripts/package.py expected

upstream: ## Download and check upstream's archives and sidecars
	python3 scripts/verify_upstream.py --work-dir upstream-dist

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-16s %s\n", $$1, $$2}'
