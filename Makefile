.PHONY: help all prepare test lint ruff fmt check-fmt markdownlint matrix expected upstream

# Every uv call goes through the vendored helper, which cleans the environment,
# uses the global uv cache, runs offline first and goes online at most once. See
# https://github.com/leynos/shared-actions/tree/main/uv_gate for the contract.
UV_GATE ?= python3 scripts/uv_gate.py
PYTHON_VERSION ?= 3.13
RUFF_VERSION ?= 0.15.12
PY_SOURCES := scripts tests
MDLINT ?= markdownlint-cli2
# `make fmt` and `make check-fmt` call mdtablefix directly. `--git` selects the
# Markdown files Git tracks and `--include-untracked` adds the untracked files
# Git does not ignore, so a new document is formatted before it is staged.
# Both modes need mdtablefix 0.6.0 or later; CI pins the version at the
# install-mdtablefix step.
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_SELECT = --git --include-untracked
MDTABLEFIX_RULES = --wrap --renumber --breaks --ellipsis --fences

all: check-fmt lint test ## Run every commit gate

prepare: ## Install the locked development environment, offline first
	$(UV_GATE) prepare --python $(PYTHON_VERSION) --group dev

test: prepare ## Run the unit tests, property tests and workflow contracts
	$(UV_GATE) run --python $(PYTHON_VERSION) --group dev -- python -m pytest -q

lint: markdownlint ruff ## Lint Python and Markdown sources

ruff: ## Lint Python sources
	$(UV_GATE) tool --from ruff@$(RUFF_VERSION) -- ruff check $(PY_SOURCES)

markdownlint: ## Lint Markdown files
	$(MDLINT) "**/*.md" "#.venv"

fmt: ## Format Python sources
	$(UV_GATE) tool --from ruff@$(RUFF_VERSION) -- ruff format $(PY_SOURCES)
	$(UV_GATE) tool --from ruff@$(RUFF_VERSION) -- ruff check --fix $(PY_SOURCES)
	$(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)
	@unset FORCE_COLOR; $(MDLINT) --fix "**/*.md"

check-fmt: ## Verify Python formatting
	$(UV_GATE) tool --from ruff@$(RUFF_VERSION) -- ruff format --check $(PY_SOURCES)
	$(MDTABLEFIX) --check $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)

matrix: ## Print the release build matrix derived from dylint.toml
	python3 scripts/matrix.py matrix

expected: ## Print the asset names a release must publish
	python3 scripts/package.py expected

upstream: ## Download and check upstream's archives and sidecars
	python3 scripts/verify_upstream.py --work-dir upstream-dist

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-16s %s\n", $$1, $$2}'
