# health-monitor monitoring server + TUI
#
# The venv is deliberately this app's own (.venvmonitor).  Do not point
# any of this at ~/venv3 -- that is the vLLM environment and its torch
# install is easy to break and slow to rebuild.

VENV    := .venvmonitor
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
STAMP   := $(VENV)/.deps-installed
PORT    ?= 5678
DATADIR ?= $(HOME)/.health-monitor
VLLM    ?= http://127.0.0.1:8000

# uv is not installed on this box; plain venv + pip is one less thing to
# depend on.
PYTHON3 ?= python3

.PHONY: help venv deps serve tui reset clean check

help:
	@echo "make serve   - run the monitoring server on port $(PORT)"
	@echo "make tui     - run the TUI (requires a running server)"
	@echo "make reset   - rotate the samples database and start a fresh one"
	@echo "make venv    - create $(VENV) if missing"
	@echo "make clean   - remove $(VENV)"
	@echo ""
	@echo "vars: PORT=$(PORT)  DATADIR=$(DATADIR)  VLLM=$(VLLM)"

$(PY):
	@echo ">> creating $(VENV)"
	@$(PYTHON3) -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip

$(STAMP): $(PY) requirements.txt
	@echo ">> installing dependencies"
	@$(PIP) install --quiet -r requirements.txt
	@touch $(STAMP)

venv: $(STAMP)

deps: $(STAMP)

serve: $(STAMP)
	@$(PY) -m health_monitor serve --port $(PORT) --data-dir $(DATADIR) --vllm $(VLLM)

tui: $(STAMP)
	@$(PY) -m health_monitor --server http://127.0.0.1:$(PORT)

reset: $(STAMP)
	@$(PY) -m health_monitor serve-reset --data-dir $(DATADIR)

check: $(STAMP)
	@$(PY) -m compileall -q health_monitor && echo "compile ok"

clean:
	rm -rf $(VENV)
