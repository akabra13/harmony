# Convenience wrapper. `make` is optional and not the documented path — a reviewer
# on Windows does not have it, and a headline command that works on two platforms
# out of three is not one command. Everything here is a shortcut for something the
# `harmony` console script already does:
#
#     pip install -e ".[dev]"     then     harmony demo all
#
# Every target runs in replay mode: no API key, no cost, no network.

PYTHON ?= python
VENV   ?= .venv
BIN     = $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN     = $(VENV)/Scripts
endif

PY      = $(BIN)/python
HARMONY = $(PY) -m harmony.cli.main

export PYTHONIOENCODING = utf-8
export HARMONY_LLM     ?= replay

.PHONY: help install demo demo-a demo-b demo-failures test test-fast \
        eval eval-live cassettes record recorded-run serve clean

help:
	@echo "make install       create a virtualenv and install the package"
	@echo "make demo          every scenario, in order  (= harmony demo all)"
	@echo "make demo-a        Scenario A only — detect, approve, execute, follow up"
	@echo "make demo-b        Scenario B only — the free-form path"
	@echo "make demo-failures the eight failure cases"
	@echo "make test          the whole suite, including architecture tests"
	@echo "make eval          recommendation quality against the golden cases"
	@echo "make eval-live     the same cases against the real model (needs a key)"
	@echo "make serve         the approval surface over HTTP"
	@echo "make cassettes     regenerate cassettes from the scripted fixtures"
	@echo "make record        regenerate cassettes from a LIVE model (needs a key)"
	@echo "make recorded-run  write docs/runs/scenario-a.md from the audit log"

install:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"
	@echo "installed. now run: make demo   (or: $(BIN)/harmony demo all)"

# --- the demos -----------------------------------------------------------------

demo:
	$(HARMONY) demo all

demo-a:
	$(HARMONY) demo run scenario-a

demo-b:
	$(HARMONY) demo run scenario-b

demo-failures:
	$(HARMONY) demo run failures

# --- tests and evaluation ------------------------------------------------------

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest -m "not integration"

eval:
	$(HARMONY) eval

eval-live:
	HARMONY_LLM=live $(HARMONY) eval --live --verbose

# --- serving -------------------------------------------------------------------

serve:
	$(HARMONY) init --db .harmony/serve.db --force
	$(HARMONY) run --user u-101 --db .harmony/serve.db
	$(HARMONY) serve --db .harmony/serve.db

# --- cassettes and the recorded run --------------------------------------------

cassettes:
	$(PY) scripts/author_cassettes.py

record:
	HARMONY_LLM=record $(PY) scripts/author_cassettes.py

recorded-run:
	$(PY) scripts/write_recorded_run.py

clean:
	rm -rf .harmony .pytest_cache .coverage
	find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
