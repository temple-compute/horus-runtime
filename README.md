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

For detailed documentation on internationalization and SDK usage, see the [horus-runtime SDK i18n guide](https://docs.templecompute.com/sdk/i18n).

## Funding & Credits

- Developed by the [Barcelona Supercomputing Center](https://www.bsc.es/)
- © BSC. All rights reserved.

## License

`horus-runtime` is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the [LICENSE](LICENSE) file for details.

For commercial licensing and support, please contact us at [christian.dominguez@templecompute.com](mailto:christian.dominguez@templecompute.com)
