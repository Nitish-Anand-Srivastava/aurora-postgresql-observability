# Cross-platform via `python` on PATH; on Windows use PowerShell or WSL with `make`, or just call
# `python scripts/validate.py` directly (see README.md / CONTRIBUTING.md).
PYTHON ?= python3

.PHONY: validate validate-yaml validate-json validate-dashboard validate-prometheus \
        validate-shell validate-markdown validate-secrets validate-simulator validate-setup install-dev clean-tools

validate:
	$(PYTHON) scripts/validate.py

validate-yaml:
	$(PYTHON) scripts/validate.py --only yaml

validate-json:
	$(PYTHON) scripts/validate.py --only json

validate-dashboard:
	$(PYTHON) scripts/validate.py --only dashboard

validate-prometheus:
	$(PYTHON) scripts/validate.py --only prometheus

validate-shell:
	$(PYTHON) scripts/validate.py --only shell

validate-markdown:
	$(PYTHON) scripts/validate.py --only markdown

validate-secrets:
	$(PYTHON) scripts/validate.py --only secrets

validate-simulator:
	$(PYTHON) scripts/validate.py --only simulator

validate-setup:
	$(PYTHON) scripts/validate.py --only setup

install-dev:
	$(PYTHON) -m pip install -r scripts/requirements.txt

clean-tools:
	rm -rf tools/
