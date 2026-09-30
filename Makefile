# StreamBridge
#
# Thin wrappers around the same commands documented in CONTRIBUTING.md. The
# point is not to be shorter than typing the command, it is to be the same on
# every machine: a contributor should not have to know whether the venv is
# named .venv here and venv there.
#
# Run `make help` for the list.

SHELL := /bin/bash
PY    ?= .venv/bin/python
RUFF  ?= ruff
MYPY  ?= mypy

.DEFAULT_GOAL := help
.PHONY: help setup venv test unit integration live lint format typecheck check \
        verify privacy security clean build run install uninstall

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup
setup: venv ## Create the virtual environment and install everything
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'
	@echo "Ready. Next: make test"

venv: ## Create the virtual environment only
	@test -d .venv || python3 -m venv .venv

# ---------------------------------------------------------------- tests
test: ## Run the offline test suite (no network)
	$(PY) -m pytest -q

unit: ## Run only the unit tests
	$(PY) -m pytest tests/unit -q

integration: ## Run only the integration tests
	$(PY) -m pytest tests/integration -q

live: ## Run the networked end-to-end check (needs MPD and yt-dlp)
	./scripts/e2e-test.sh

browser: ## Run the headless-browser UI tests (needs playwright)
	$(PY) -m pytest tests/browser -q

# ---------------------------------------------------------------- quality
lint: ## ruff check
	$(RUFF) check .

format: ## ruff format
	$(RUFF) format .

typecheck: ## mypy
	$(MYPY)

check: lint typecheck ## Everything that must pass before a commit

verify: ## The full gate: lint, types, tests, privacy
	./scripts/verify.sh

privacy: ## Scan the tree and the whole history for personal data
	./scripts/privacy-audit.sh --all-commits

security: ## Security audit (add --offline to skip the network)
	./scripts/security-audit.sh

# ---------------------------------------------------------------- run
run: ## Start the server in the foreground
	$(PY) -m streambridge.server

doctor: ## Check the local environment
	$(PY) -m streambridge.cli doctor

install: ## Install into ~/.local
	./scripts/install.sh

uninstall: ## Remove the installation
	./scripts/uninstall.sh --yes

# ---------------------------------------------------------------- build
build: ## Build the wheel and sdist into dist/
	$(PY) -m build

clean: ## Remove build and cache artefacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
