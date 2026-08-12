# Contributing to Horus Runtime

Thanks for taking the time to contribute. Bug reports, docs fixes, plugins, and features
are all welcome.

## Ways to help

- **Report a bug**: [open an issue](https://github.com/temple-compute/horus-runtime/issues/new/choose)
  with a minimal `workflow.yaml` that reproduces it.
- **Pick up an issue**: start with
  [good first issues](https://github.com/temple-compute/horus-runtime/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
- **Ask or propose something**: use
  [Discussions](https://github.com/temple-compute/horus-runtime/discussions) before large
  changes, so we can agree on the approach first.
- **Write a plugin**: you usually do not need to change this repo at all. See
  [the plugin template](https://github.com/temple-compute/horus-runtime-plugin).

## Development setup

Requirements: Python 3.13 or 3.14.

With `uv` (what CI uses):

```bash
git clone https://github.com/temple-compute/horus-runtime.git
cd horus-runtime
uv sync --group dev
```

With micromamba and pip:

```bash
micromamba create -y -n horus_runtime python=3.14
micromamba activate horus_runtime
pip install -e ".[dev]"
```

Install the git hooks, which run lint, types, translations, and license headers on commit:

```bash
pre-commit install
```

## Everyday commands

| Command | Purpose |
| --- | --- |
| `make test` | Run the full test suite with coverage (same invocation as CI). |
| `make lint` | Ruff lint, Ruff format check, and mypy (same as CI). |
| `make format` | Auto-fix and format with Ruff. |
| `make type-check` | mypy only. |
| `make add-license-headers` | Apply AGPL headers to `src` and `tests`. |
| `make babel-check` | Verify no missing or fuzzy translations. |
| `make help` | Everything else. |

## What CI enforces

Your PR must pass all of these, so run them locally first:

- **Ruff** lint and format, at a 79 character line length.
- **mypy in strict mode** over `src` and `tests`. New code needs type annotations.
- **Tests** on Python 3.13 and 3.14, with a **90% coverage floor**.
- **License headers**: CI runs `make add-license-headers` and fails if it changes any
  file, so run it before pushing.
- **Translations**: `make babel-check` must report every string translated.

## Internationalization

User-facing strings go through gettext. Import the translation helper and wrap them:

```python
from horus_runtime.i18n import tr as _

message = _("Hello, world!")

# Plurals and substitution
message = _("{n} notification", "{n} notifications", n=2)
```

After adding or changing strings, refresh the catalogs and translate the new entries
(currently `es`), then verify:

```bash
make babel-refresh
make babel-check
```

Details in the [SDK i18n guide](https://docs.templecompute.com/sdk/i18n).

## Pull requests

- Branch off `main` and keep the change focused on one thing.
- Add tests for behaviour changes.
- Fill in the PR template. **If a user, plugin, or GUI can observe your change, it needs
  documentation**, and the template asks you to link the corresponding
  [horus-docs](https://github.com/temple-compute/horus-docs) PR.
- Green CI is required before review.

## License

By contributing you agree that your contributions are licensed under the
[AGPL-3.0](LICENSE), the same license as the project.
