# horus-runtime

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%20%E2%80%94%203.14-blue.svg)](https://www.python.org/downloads/)

horus-runtime is the command-line engine for the Temple Compute platform, a workflow manager designed for High Performance Computing. It enables the creation, management, and execution of complex workflows, especially scientific computing, without any GUI dependencies.

- **Modular & Extensible:** Designed for integration, automation, and extension via Python.
- **Open & Free:** Developed by Temple Compute.

## Features

- **Workflow Execution:** Run and manage scientific workflows from the terminal.
- **Modular Blocks:** Compose workflows from reusable, autonomous blocks.
- **Remote Execution:** Configure targets to offload calculations.
- **Extensible:** Build and share your own blocks and extensions using the Python API.
- **Reproducibility:** Share and version your workflows, including all state and configuration.

<img width="1600" height="1202" alt="Horus Runtime TUI" src="https://github.com/user-attachments/assets/8f6a5f77-c0fa-48bf-b82b-5683af1caec4" />

## Workflows Library

Check out the [Pantheon](https://github.com/temple-compute/pantheon/) repository for a curated library of production-ready workflows, from drug discovery (Boltz-2 virtual screening, AutoDock Vina docking) to molecular dynamics setup with BioExcel Building Blocks (GROMACS, AMBER). Contributions are welcomed.

## Development

### Requirements

- Python 3.13–3.14
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

To commit to the repo, you'll need the pre-commit package:

```bash
pip install pre-commit
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

- Developed by [Temple Compute](www.templecompute.com)

## License

`horus-runtime` is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the [LICENSE](LICENSE) file for details.

For commercial licensing and support, please contact Temple Compute at [christian@templecompute.com](mailto:christian@templecompute.com)
