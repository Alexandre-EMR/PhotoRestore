# PhotoRestore — Makefile
# `make replay` regenerates the synthetic corpus and replays the whole
# pipeline in dry-run from scratch: the reproducibility contract.

PY ?= python3

.PHONY: help test lint replay clean

help:
	@echo "make test    — unittest suite"
	@echo "make lint    — static checks (ruff when available, else skip)"
	@echo "make replay   — regenerate fixtures + full dry-run pipeline + checks"
	@echo "make clean    — remove generated artifacts"

test:
	$(PY) -m unittest discover -s tests -v

lint:
	@command -v ruff >/dev/null 2>&1 && ruff check photorestore tests || \
	  echo "ruff absent — skip (zero-dependency policy)"

replay: clean test
	$(PY) tests/make_fixtures.py .fixtures
	$(PY) -m photorestore.cli --db .replay.db --report-dir .replay_reports run .fixtures
	$(PY) -c "import csv;rows=list(csv.reader(open('.replay_reports/match_report.csv')));print('replay OK —', len(rows)-1, 'décisions journalisées')"

clean:
	rm -rf .fixtures .replay.db .replay.db-wal .replay.db-shm .replay_reports
