# Makefile for Horus Runtime development

# Command definitions (used in CI and locally)
PYTEST_CMD = pytest --cov --cov-report=xml --cov-report=html --cov-report=term --junitxml=test-results.xml
BLACK_CHECK_CMD = black --check --diff .
ISORT_CHECK_CMD = isort --check-only --diff .
PYLINT_CMD = pylint src/ --output-format=text
FLAKE8_CMD = flake8 src/
PYRIGHT_CMD = pyright
BLACK_FORMAT_CMD = black .
ISORT_FORMAT_CMD = isort .
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

.PHONY: install test test-unit test-simple docs lint format type-check clean help black-check isort-check pylint-check flake8-check add-license-headers babel-update babel-check babel-add babel-extract

help:
	@echo "Available commands:"
	@echo "  install      Install micromamba horus_runtime environment and dependencies"
	@echo "  test         Run all tests with coverage (same as CI)"
	@echo "  test-unit    Run unit tests only"
	@echo "  test-int     Run integration tests only"
	@echo "  test-simple  Run tests without coverage"
	@echo "  docs         Generate documentation with pdoc"
	@echo "  lint         Run all linting tools (same as CI)"
	@echo "  black-check  Check code formatting (used by CI)"
	@echo "  isort-check  Check import sorting (used by CI)"
	@echo "  pylint-check Check with pylint (used by CI)"
	@echo "  flake8-check Check with flake8 (used by CI)"
	@echo "  format       Format code with black and isort"
	@echo "  type-check   Run type checking"
	@echo "  add-license-headers  Add license headers to source files"
	@echo "  babel-update  Update Babel translations"
	@echo "  babel-check   Check Babel translations (used by CI)"
	@echo "  babel-add     Add a new language (usage: make babel-add LANG=es)"
	@echo "  babel-extract Extract translatable strings to messages.pot"
	@echo "  clean        Remove cache files"

install:
	micromamba create -y -n horus_runtime python=3.11
	micromamba activate horus_runtime
	pip install -e ".[dev,docs]"

test:
	$(PYTEST_CMD)

test-unit:
	pytest tests/unit -m unit

test-int:
	pytest tests/integration -m integration

test-simple:
	pytest

# This will generate a "preview" version of the docs by default.
# To generate a different version, pass the version as an argument, e.g.:
# make docs VERSION=beta
docs:
	rm -rf docs/
	pydoc-markdown

# Individual check commands (used by CI)
black-check:
	$(BLACK_CHECK_CMD)

isort-check:
	$(ISORT_CHECK_CMD)

pylint-check:
	$(PYLINT_CMD)

flake8-check:
	@$(FLAKE8_CMD)

lint:
	$(BLACK_CHECK_CMD)
	$(ISORT_CHECK_CMD)
	$(PYLINT_CMD)
	$(FLAKE8_CMD)
	$(PYRIGHT_CMD)

format:
	$(BLACK_FORMAT_CMD)
	$(ISORT_FORMAT_CMD)

type-check:
	$(PYRIGHT_CMD)

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
