# Contributing to PyHako

Thank you for your interest in contributing!

## Development Setup

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/xebjhm/PyHako.git
    cd PyHako
    ```

2.  **Install dependencies** using `uv`:
    ```bash
    uv sync
    ```

3.  **Run Tests**:
    ```bash
    uv run pytest
    ```

## Coding Standards

- **Linting**: We use `ruff`. Run `uv run ruff check .` before committing.
- **Formatting**: We use `ruff format`.
- **Type Hints**: All new code must be fully typed. Run `uv run mypy .`.

## Pull Requests

1.  Fork the repo and create your branch from `main`.
2.  Add tests for your changes.
3.  Ensure the test suite passes.
4.  Open a Pull Request.

## License
By contributing, you agree that your contributions will be licensed under its MIT License.
