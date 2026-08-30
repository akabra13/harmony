# Harmony — an extendable agent harness for enterprise work.
#
# `make demo` is the one documented command. Everything else is a shortcut.

PYTHON ?= python
VENV   ?= .venv
BIN     = $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN     = $(VENV)/Scripts
endif

PY      = $(BIN)/python
DEMO_DB = .harmony/demo.db

# The demo replays recorded model exchanges: no API key, no cost, and the same
# result every time. Set HARMONY_LLM=live to call the model for real.
export PYTHONIOENCODING = utf-8
export HARMONY_LLM     ?= replay

.PHONY: help install demo demo-a demo-b demo-failures test test-fast \
        lint cassettes record recorded-run clean check

help:
	@echo "make install       create a virtualenv and install the package"
	@echo "make demo          Scenario A, Scenario B, then the failure suite"
	@echo "make demo-a        Scenario A only — detect, approve, execute, follow up"
	@echo "make demo-b        Scenario B only — the free-form path"
	@echo "make demo-failures the eight failure cases"
	@echo "make test          the whole suite, including architecture tests"
	@echo "make check         tests + the structural claims the README makes"
	@echo "make eval          check recommendation quality against golden cases"
	@echo "make eval-live     the same cases against the real model (needs a key)"
	@echo "make cassettes     regenerate cassettes from the scripted fixtures"
	@echo "make record        regenerate cassettes from a LIVE model (needs a key)"
	@echo "make recorded-run  write docs/runs/scenario-a.md from the audit log"

install:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"
	@echo "installed. now run: make demo"

# --- the demos -----------------------------------------------------------------

demo: demo-a demo-b demo-failures

demo-a:
	$(PY) -m harmony.cli.main demo run scenario-a --db $(DEMO_DB)

demo-b:
	$(PY) -m harmony.cli.main demo run scenario-b --db $(DEMO_DB)

demo-failures:
	$(PY) -m harmony.cli.main demo run failures --db $(DEMO_DB)

# --- tests ---------------------------------------------------------------------

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest -m "not integration"

# The claims the README makes, checked rather than asserted.
check: test
	@echo
	@echo "--- the kernel knows nothing about manufacturing ---"
	@! grep -rIl --include=*.py -E "\b(purchase_order|supplier|part_id|northfield)\b" harmony/ \
	  || (echo "FAIL: domain vocabulary in the kernel" && exit 1)
	@echo "ok — no domain vocabulary in harmony/"
	@echo
	@echo "--- the audit chain verifies ---"
	@$(PY) -m harmony.cli.main audit verify --db $(DEMO_DB) 2>/dev/null \
	  || echo "(run make demo first)"

# --- evaluation ----------------------------------------------------------------

eval:
	$(PY) -m harmony.cli.main eval

eval-live:
	HARMONY_LLM=live $(PY) -m harmony.cli.main eval --live --verbose

# --- cassettes and the recorded run --------------------------------------------

cassettes:
	$(PY) scripts/author_cassettes.py

record:
	HARMONY_LLM=record $(PY) scripts/author_cassettes.py

recorded-run:
	$(PY) scripts/write_recorded_run.py

# --- housekeeping --------------------------------------------------------------

clean:
	rm -rf .harmony .pytest_cache **/__pycache__ .coverage
	find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
