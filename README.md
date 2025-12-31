# horus-runtime

horus-runtime is the command-line engine for the Horus platform, a free, multi-platform workflow manager designed for developers and scientists. It enables the creation, management, and execution of complex workflows, especially in biomolecular modeling and scientific computing, without any GUI dependencies.

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

- Python 3.9–3.14 (recommended: 3.14)
- [micromamba](https://mamba.readthedocs.io/en/latest/user_guide/micromamba.html) (for environment management, optional but recommended)

### Environment

```bash
# Create and activate environment (recommended)
micromamba create -y -n horus_runtime python=3.14
micromamba activate horus_runtime

# Install dependencies
pip install -r requirements.txt
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

## Funding & Credits

- Developed by the [Barcelona Supercomputing Center](https://www.bsc.es/)
- © BSC. All rights reserved.

## License

See [LICENSE](LICENSE) for details.
