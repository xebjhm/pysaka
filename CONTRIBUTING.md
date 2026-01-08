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

## Testing

### Unit Tests (CI Default)
Unit tests run automatically in CI and exclude integration tests:
```bash
uv run pytest tests/
```

### Integration Tests (Local Only)
Integration tests require **prior login** with the CLI. They test real browser refresh and API calls.

**Prerequisites**:
1. Login first: `pyhako-cli -s <group>` (run interactively)
2. Verify auth_data exists: `~/.local/share/pyhako/auth_data`

**Run integration tests**:
```bash
uv run pytest tests/test_integration.py -m integration -v
```

### CLI Build Testing (Shift-Left)
Test the PyInstaller build locally before pushing to CI.

**Build locally**:
```bash
cd ../PyHakoCLI
uv run python scripts/build_local.py
```

**Smoke test**:
```bash
uv run python scripts/test_build.py
# Or test directly:
./dist/pyhako-cli --help
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
