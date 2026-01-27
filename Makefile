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

.PHONY: install test lint format type-check clean help black-check isort-check pylint-check flake8-check add-license-headers

help:
	@echo "Available commands:"
	@echo "  install      Install micromamba horus_runtime environment and dependencies"
	@echo "  test         Run all tests with coverage (same as CI)"
	@echo "  test-unit    Run unit tests only"
	@echo "  test-int     Run integration tests only"
	@echo "  test-simple  Run tests without coverage"
	@echo "  lint         Run all linting tools (same as CI)"
	@echo "  black-check  Check code formatting (used by CI)"
	@echo "  isort-check  Check import sorting (used by CI)"
	@echo "  pylint-check Check with pylint (used by CI)"
	@echo "  flake8-check Check with flake8 (used by CI)"
	@echo "  format       Format code with black and isort"
	@echo "  type-check   Run type checking"
	@echo "  add-license-headers  Add license headers to source files"
	@echo "  clean        Remove cache files"

install:
	micromamba create -y -n horus_runtime python=3.14
	micromamba activate horus_runtime
	pip install -r requirements.txt
	pip install -e .

test:
	$(PYTEST_CMD)

test-unit:
	pytest tests/unit -m unit

test-int:
	pytest tests/integration -m integration

test-simple:
	pytest

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
