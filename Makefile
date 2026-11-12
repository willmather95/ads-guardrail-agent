PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install demo test hero fixtures clean

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q -e '.[dev]'

install: $(BIN)/python

demo: install            ## SIMULATED 14-day run on SYNTHETIC fixtures, no credentials
	$(BIN)/python -m ads_guardrail demo --out demo/out

test: install
	$(BIN)/python -m pytest

hero: demo               ## re-render docs/hero.png from demo/out/summary.json (needs Chrome)
	cp demo/out/summary.json docs/demo-summary.json && cp demo/out/timeline.csv docs/demo-timeline.csv
	$(BIN)/python scripts/render_hero.py docs/demo-summary.json docs/hero.png

fixtures: install
	$(BIN)/python -m ads_guardrail fixtures

clean:
	rm -rf demo/out .ads-guardrail .pytest_cache
