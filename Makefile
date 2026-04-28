# Common dev tasks for brainstack.
# Use `make help` to list targets.

# Python interpreter (override: make PY=...)
PY ?= /opt/homebrew/bin/python3.13

# Test runner (defaults to a venv at /tmp/agentic-venv if it exists, else $PY -m pytest)
PYTEST_BIN := $(shell test -x /tmp/agentic-venv/bin/pytest && echo /tmp/agentic-venv/bin/pytest || echo $(PY) -m pytest)

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?##"} {printf "%-20s %s\n", $$1, $$2}'

.PHONY: test
test: ## Run the full test suite
	$(PYTEST_BIN) tests/ -v

.PHONY: test-quick
test-quick: ## Run tests with short summary
	$(PYTEST_BIN) tests/ -q

.PHONY: test-fuzz
test-fuzz: ## Run only the fuzz tests
	$(PYTEST_BIN) tests/test_redact_jsonl_fuzz.py tests/test_concurrent_appends.py -v

.PHONY: test-both-pythons
test-both-pythons: ## Run tests on stock python3 and the venv python3.13
	@echo "=== Python 3.9 (system) ==="
	python3 -m pytest tests/ -q
	@echo ""
	@echo "=== Python 3.13 (venv) ==="
	/tmp/agentic-venv/bin/pytest tests/ -q

.PHONY: scan
scan: ## Run redact.py against the live brain
	$(PY) ~/.agent/tools/redact.py ~/.agent

.PHONY: scrub
scrub: ## Run redact_jsonl.py against the live brain (in-place)
	$(PY) ~/.agent/tools/redact_jsonl.py ~/.agent/memory/episodic ~/.agent/data-layer

.PHONY: install
install: ## Fresh install of ~/.agent
	PYTHON_BIN=$(PY) ./install.sh

.PHONY: upgrade
upgrade: ## Re-sync code into ~/.agent (memory data left untouched)
	PYTHON_BIN=$(PY) ./install.sh --upgrade

.PHONY: verify
verify: ## Self-check the brain layout
	PYTHON_BIN=$(PY) ./install.sh --verify

.PHONY: dream
dream: ## Run the dream cycle once
	$(PY) ~/.agent/tools/dream_runner.py

.PHONY: clean
clean: ## Remove pyc / __pycache__ / pytest cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete

.PHONY: lint
lint: ## Static checks (best-effort — no hard dependency)
	@command -v ruff >/dev/null && ruff check agent/ tests/ || echo "ruff not installed; skipping"
	@$(PY) -m py_compile $$(find agent -name '*.py') && echo "py_compile: ok"

.PHONY: report-status
report-status: ## Print quick health: tests + brain scan
	@echo "=== Tests ==="
	@$(PYTEST_BIN) tests/ -q --tb=no 2>&1 | tail -3
	@echo ""
	@echo "=== Brain scan ==="
	@$(PY) ~/.agent/tools/redact.py ~/.agent && echo "BRAIN CLEAN" || echo "BRAIN HAS HITS"
