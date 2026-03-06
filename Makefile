# Makefile for Horus Runtime development

# Command definitions (used in CI and locally)
PYTEST_CMD = pytest --cov --cov-report=xml --cov-report=html --cov-report=term --junitxml=test-results.xml
RUFF_LINT_CMD = ruff check src/ tests/
RUFF_FORMAT_CHECK_CMD = ruff format --check --diff .
RUFF_FIX_CMD = ruff check --fix src/ tests/
RUFF_FORMAT_CMD = ruff format .
MYPY_CMD = mypy src/ tests/
ADD_LICENSE_HEADERS_CMD = licenseheaders -t .agpl3.tmpl -cy -o 'Temple Compute' -n horus-runtime -u https://horus.bsc.es

# i18n settings (babel)
BABEL_CFG = babel.cfg
LOCALE_DIR = src/horus_runtime/locale
MESSAGES_POT = $(LOCALE_DIR)/messages.pot
DOMAIN = horus_runtime
SOURCE_DIR = src/horus_runtime

# Variables used for babel metadata
PROJECT_NAME = horus-runtime
ORGANIZATION = Temple Compute

.PHONY: install test lint format type-check clean help ruff-check ruff-format-check mypy-check add-license-headers babel-update babel-check babel-add babel-extract

help:
	@echo "Available commands:"
	@echo "  install             Install micromamba horus_runtime environment and dependencies"
	@echo "  test                Run all tests with coverage (same as CI)"
	@echo "  lint                Run all linting tools (same as CI)"
	@echo "  ruff-check          Check code with ruff linter (used by CI)"
	@echo "  ruff-format-check   Check code formatting with ruff (used by CI)"
	@echo "  mypy-check          Check types with mypy (used by CI)"
	@echo "  format              Format code with ruff"
	@echo "  type-check          Run type checking"
	@echo "  add-license-headers Add license headers to source files"
	@echo "  babel-update        Update Babel translations"
	@echo "  babel-check         Check Babel translations (used by CI)"
	@echo "  babel-add           Add a new language (usage: make babel-add LANG=es)"
	@echo "  babel-extract       Extract translatable strings to messages.pot"
	@echo "  clean               Remove cache files"

install:
	micromamba create -y -n horus_runtime python=3.11
	micromamba activate horus_runtime
	pip install -e ".[dev]"

test:
	# Set PYTHONPATH to current directory to ensure tests can find other test modules
	PYTHONPATH=. $(PYTEST_CMD)

# Individual check commands (used by CI)
ruff-check:
	$(RUFF_LINT_CMD)

ruff-format-check:
	$(RUFF_FORMAT_CHECK_CMD)

mypy-check:
	$(MYPY_CMD)

lint:
	$(RUFF_LINT_CMD)
	$(RUFF_FORMAT_CHECK_CMD)
	$(MYPY_CMD)

format:
	$(RUFF_FIX_CMD)
	$(RUFF_FORMAT_CMD)

type-check:
	$(MYPY_CMD)

add-license-headers:
	$(ADD_LICENSE_HEADERS_CMD) -d src
	$(ADD_LICENSE_HEADERS_CMD) -d tests

clean:
	find . -type d -name "__pycache__" -delete
	find . -type f -name "*.pyc" -delete
	rm -rf .coverage htmlcov/ .pytest_cache/
	rm -rf *.egg-info/ build/ dist/

babel-extract:
	pybabel extract -F $(BABEL_CFG) \
		--project=$(PROJECT_NAME) \
		--copyright-holder="$(ORGANIZATION)" \
		-o $(MESSAGES_POT) $(SOURCE_DIR)

babel-update:
	pybabel update -i $(MESSAGES_POT) -d $(LOCALE_DIR) -D $(DOMAIN) --no-fuzzy-matching

babel-refresh: babel-extract babel-update

babel-check:
	@echo "Checking for missing or fuzzy translations..."
	@for file in $(shell find $(LOCALE_DIR) -name "*.po"); do \
		RESULT=$$(msgfmt --statistics -c -o /dev/null $$file 2>&1); \
		echo "$$RESULT"; \
		if echo "$$RESULT" | grep -E "untranslated|fuzzy" > /dev/null; then \
			echo "ERROR: $$file has missing or fuzzy strings!"; \
			exit 1; \
		fi; \
	done
	@echo "Success: All strings are translated."
	pybabel compile -d $(LOCALE_DIR) -D $(DOMAIN) --statistics

babel-add:
	pybabel init -i $(MESSAGES_POT) -d $(LOCALE_DIR) -l $(LANG) -D $(DOMAIN)
