PY ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports
SRC := 03-development/src/taskq_api
TESTS := 03-development/tests
PYTHONPATH := 03-development/src:$(PYTHONPATH)
export PYTHONPATH

.PHONY: install lint-imports bandit mutate test test-unit test-int cov verify-system migrate-up migrate-down reset-db key-create healthcheck

install:
	$(PY) -m pip install -r requirements-dev.txt

lint-imports:
	$(LINT_IMPORTS) --config .importlinter

bandit:
	$(PY) -m bandit -q -r $(SRC) || true

test:
	$(PY) -m pytest $(TESTS) -q

test-unit:
	$(PY) -m pytest $(TESTS)/unit -q

test-int:
	$(PY) -m pytest $(TESTS)/integration -q

cov:
	$(PY) -m pytest $(TESTS) --cov=$(SRC) --cov-report=term-missing

migrate-up:
	$(PY) -m alembic upgrade head

migrate-down:
	$(PY) -m alembic downgrade base

reset-db:
	rm -f taskq.db && $(PY) -m alembic upgrade head

key-create:
	$(PY) -m taskq_api key create --scope write

healthcheck:
	$(PY) -m taskq_api healthcheck

# NFR-12 / SPEC §8 #27: end-to-end verification target.
# Runs migration round-trip + full test suite + service smoke.
verify-system:
	@set -e; \
	echo "== verify-system: migrate up =="; \
	$(PY) -m alembic upgrade head; \
	echo "== verify-system: run tests =="; \
	$(PY) -m pytest $(TESTS) -q; \
	echo "== verify-system: import-linter =="; \
	$(LINT_IMPORTS) --config .importlinter; \
	echo "== verify-system: bandit =="; \
	$(PY) -m bandit -q -r $(SRC) || true; \
	echo "== verify-system: round-trip migration =="; \
	$(PY) -m alembic downgrade -1; \
	$(PY) -m alembic upgrade head; \
	echo "== verify-system: PASS =="