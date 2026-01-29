# horus-runtime

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%E2%80%94%203.14-blue.svg)](https://www.python.org/downloads/)

horus-runtime is the command-line engine for the Horus platform, a free, multi-platform workflow manager designed for developers and scientists. It enables the creation, management, and execution of complex workflows, especially scientific computing, without any GUI dependencies.

- **No GUI:** This package is the runtime/engine only. For the full graphical experience, see the main Horus project.
- **Modular & Extensible:** Designed for integration, automation, and extension via Python.
- **Open & Free:** Developed by the Barcelona Supercomputing Center.

## Features

- **Workflow Execution:** Run and manage scientific workflows from the terminal.
- **Modular Blocks:** Compose workflows from reusable, autonomous blocks.
- **Remote Execution:** Configure SSH remotes to offload calculations.
- **Extensible:** Build and share your own blocks and extensions using the Python API.
- **Reproducibility:** Share and version your workflows, including all state and configuration.

## Components

![horus-runtime components](https://i.ibb.co/m5J0j2ZJ/531204675-95555200-e1ea-4684-9922-949cb34742e2.png)

## Development

### Requirements

- Python 3.10–3.14 (recommended: 3.14)
- [micromamba](https://mamba.readthedocs.io/en/latest/user_guide/micromamba.html) (for environment management, optional but recommended)

### Environment

```bash
# Create and activate environment (recommended)
micromamba create -y -n horus_runtime python=3.14
micromamba activate horus_runtime
```

Install the package in editable (development) mode to enable local development alongside dependencies:

```bash
pip install -e .[dev]
```

### Command Shortcuts

- **Run all tests:**
  ```bash
  make test
  ```
- **Lint and type-check:**
  ```bash
  make lint
  make type-check
  ```
- **Format code:**
  ```bash
  make format
  ```

See `make help` for all available commands.

## Internationalization (i18n)

Horus Runtime supports multiple languages through a comprehensive i18n system. Translations are managed using Babel and GNU gettext.

### Using Translations in Your Code

Import the translation function in your plugins:

```python
from horus_runtime.i18n import tr as _

# Use it in your code
message = _("Hello, world!")

# Supports plurals and text substitution
message = _("{n} notification", "{n} notifications", n=2)
```

### Babel Configuration

If you need to regenerate the main `messages.po` file:

1. Ensure the `src/horus_runtime/locale/` directory exists:

```bash
mkdir -p src/horus_runtime/locale/
```

2. Extract translatable strings:

```bash
make babel-extract
```

### Adding a New Language

1. **Create language files:**

   ```bash
   make babel-add LANG=es  # Replace 'es' with your language code
   ```

2. **Edit translations:**
   Edit the generated `.po` file at `src/horus_runtime/locale/LANG/LC_MESSAGES/horus_runtime.po`

3. **Validate translations:**
   ```bash
   make babel-check
   ```

### Updating Existing Translations

When translatable strings change in the source code:

1. **Extract and update:**

   ```bash
   make babel-refresh
   ```

2. **Review changes:**
   Look for "fuzzy" and new strings in `.po` files and update them

3. **Compile:**
   ```bash
   make babel-check
   ```

### Translation Management Commands

- `make babel-stats` - Show translation completion statistics
- `make babel-check` - Verify translations are up to date
- `make babel-extract` - Extract translatable strings only
- `make babel-compile` - Compile `.po` files to `.mo` files only

## Funding & Credits

- Developed by the [Barcelona Supercomputing Center](https://www.bsc.es/)
- © BSC. All rights reserved.

## License

`horus-runtime` is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the [LICENSE](LICENSE) file for details.

For commercial licensing and support, please contact us at [christian.dominguez@templecompute.com](mailto:christian.dominguez@templecompute.com)
