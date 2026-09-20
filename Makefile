# health-monitor
#
# One venv, .venvhealth, with vendor extras chosen by what is on this box:
#   NVIDIA  -> nvidia-ml-py from PyPI
#   AMD     -> amdsmi is a *system* package matched to the driver, so the
#              venv is created with --system-site-packages on AMD boxes
#   Gaudi   -> nothing extra (ctypes to libhlml)
# Never touches any other venv on the box.

VENV     := .venvhealth
PY       := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
STAMP    := $(VENV)/.deps-installed
PYTHON3  ?= python3
PKG      := health_monitor

HAS_NVIDIA := $(shell command -v nvidia-smi >/dev/null 2>&1 && echo 1)
HAS_AMD    := $(shell $(PYTHON3) -c "import amdsmi" >/dev/null 2>&1 && echo 1)
HAS_GAUDI  := $(shell test -e /usr/lib/habanalabs/libhlml.so && echo 1)
VENV_FLAGS := $(if $(HAS_AMD),--system-site-packages,)
EXTRAS     := $(if $(HAS_NVIDIA),nvidia-ml-py,)

GIT_HASH := $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
GIT_DIRTY := $(shell git diff --quiet 2>/dev/null || echo -dirty)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

.PHONY: help venv deps build monitor manager web tui check clean \
        install-units enable-units status-units vendors

help:
	@echo "targets:"
	@echo "  make monitor | manager | web | tui   run a role from this checkout"
	@echo "  make venv                          create $(VENV) with the right vendor extras"
	@echo "  make build                         write $(PKG)/_build.py (git hash + date)"
	@echo "  make install-units                 install systemd --user units for the roles"
	@echo "  make enable-units ROLES=\"monitor\"  enable + start the given roles"
	@echo "  make check                         compile everything"
	@echo ""
	@echo "detected: nvidia=$(if $(HAS_NVIDIA),yes,no) amd=$(if $(HAS_AMD),yes,no) gaudi=$(if $(HAS_GAUDI),yes,no)"
	@echo "build:    $(GIT_HASH)$(GIT_DIRTY) $(BUILD_DATE)"

vendors:
	@echo "nvidia=$(if $(HAS_NVIDIA),yes,no) amd=$(if $(HAS_AMD),yes,no) gaudi=$(if $(HAS_GAUDI),yes,no)"

$(PY):
	@echo ">> creating $(VENV) $(VENV_FLAGS)"
	@$(PYTHON3) -m venv $(VENV_FLAGS) $(VENV)
	@$(PIP) install --quiet --upgrade pip

$(STAMP): $(PY) requirements.txt
	@echo ">> installing dependencies $(if $(EXTRAS),(+ $(EXTRAS)),)"
	@$(PIP) install --quiet -r requirements.txt $(EXTRAS)
	@touch $(STAMP)

venv: $(STAMP)
deps: $(STAMP)

# The build stamp is generated, never committed: it is what the burger
# menu and --version show, so it must reflect the checkout actually
# running.
build:
	@if [ "$(GIT_HASH)" = "unknown" ] && [ -f $(PKG)/_build.py ]; then \
	  echo "build: no git here; keeping shipped $$(grep -o '"[^"]*"' $(PKG)/_build.py | head -1)"; \
	else \
	  printf 'BUILD_HASH = "%s"\nBUILD_DATE = "%s"\n' "$(GIT_HASH)$(GIT_DIRTY)" "$(BUILD_DATE)" > $(PKG)/_build.py; \
	  echo "build $(GIT_HASH)$(GIT_DIRTY) $(BUILD_DATE)"; \
	fi

check: $(STAMP)
	@$(PY) -m compileall -q $(PKG) && echo "compile ok"

monitor: $(STAMP) build
	@$(PY) -m $(PKG) monitor

manager: $(STAMP) build
	@$(PY) -m $(PKG) manager

web: $(STAMP) build
	@$(PY) -m $(PKG) web

tui: $(STAMP)
	@$(PY) -m $(PKG) tui $(TUI_ARGS)

# ---------------------------------------------------------------------- #
# systemd --user units.  No root needed; run `loginctl enable-linger $$USER`
# once (that one needs sudo) so they survive logout and start at boot.
# ---------------------------------------------------------------------- #
UNIT_DIR := $(HOME)/.config/systemd/user
ROLES    ?= monitor

install-units: $(STAMP) build
	@mkdir -p $(UNIT_DIR)
	@sed -e 's|@CHECKOUT@|$(CURDIR)|g' deploy/health-monitor@.service > $(UNIT_DIR)/health-monitor@.service
	@systemctl --user daemon-reload
	@echo "installed $(UNIT_DIR)/health-monitor@.service"
	@echo "enable with:  make enable-units ROLES=\"$(ROLES)\""
	@loginctl show-user $$USER 2>/dev/null | grep -q '^Linger=yes' || \
	  echo "NOTE: run  sudo loginctl enable-linger $$USER  once, or these stop at logout"

enable-units: install-units
	@for r in $(ROLES); do systemctl --user enable --now health-monitor@$$r.service && echo "enabled health-monitor@$$r"; done

status-units:
	@for r in monitor manager web; do systemctl --user is-active health-monitor@$$r.service 2>/dev/null | sed "s/^/$$r: /"; done

clean:
	rm -rf $(VENV) $(PKG)/_build.py
