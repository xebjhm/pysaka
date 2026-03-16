# Contributing to PyHako

Thank you for your interest in contributing to PyHako! This document outlines our development workflow, branching strategy, and contribution guidelines.

## Table of Contents

- [Git Flow Strategy](#git-flow-strategy)
- [Branch Naming Conventions](#branch-naming-conventions)
- [Development Setup](#development-setup)
- [The Changelog Rule](#the-changelog-rule)
- [Commit Message Guidelines](#commit-message-guidelines)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Code Quality Standards](#code-quality-standards)

---

## Git Flow Strategy

We follow a modified Git Flow branching model with `dev` as our integration branch.

```
main ─────●─────────────────────●─────────────────●──────► Production
           \                   /                 /
            \    release/v1.0 /                 /
             \       ●───────●                 /
              \     /         \               /
dev ───────────●───●───●───●───●───●───●───●───●──────► Integration
                \     /   \     /       \   /
                 feat/a    feat/b        hotfix/x
```

### Branch Types

| Branch | Purpose | Branch From | Merge To |
|--------|---------|-------------|----------|
| `main` | Production-ready code. Strictly versioned. Tagged releases only. | - | - |
| `dev` | Main integration branch for ongoing development. | `main` (initial) | - |
| `feat/<name>` | New features and enhancements | `dev` | `dev` via PR |
| `fix/<name>` | Bug fixes (non-urgent) | `dev` | `dev` via PR |
| `release/vX.Y.Z` | Release preparation and stabilization | `dev` | `main` AND `dev` |
| `hotfix/<name>` | Urgent production fixes | `main` | `main` AND `dev` |

---

## Branch Naming Conventions

Use lowercase with hyphens. Be descriptive but concise.

```
feat/blog-backup
feat/media-dimensions
fix/token-refresh
fix/session-expiry
release/v1.2.0
hotfix/auth-crash
```

**Prefixes:**
- `feat/` - New features
- `fix/` - Bug fixes
- `refactor/` - Code refactoring (no behavior change)
- `docs/` - Documentation only
- `test/` - Test additions or fixes
- `build/` - Build system or CI changes
- `release/` - Release preparation
- `hotfix/` - Urgent production fixes

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/xebjhm/PyHako.git
   cd PyHako
   ```

2. **Install dependencies** using `uv`:
   ```bash
   uv sync
   ```

3. **Run Tests**:
   ```bash
   uv run pytest
   ```

4. **Type Checking**:
   ```bash
   uv run mypy .
   ```

5. **Linting**:
   ```bash
   uv run ruff check .
   uv run ruff format --check .
   ```

---

## The Changelog Rule

> **Every Pull Request MUST update the `[Unreleased]` section of `CHANGELOG.md`.**

This is non-negotiable. The changelog is our historical record and release notes source.

### How to Update the Changelog

1. Open `CHANGELOG.md`
2. Find the `## [Unreleased]` section
3. Add your change under the appropriate category:

```markdown
## [Unreleased]

### Added
- New feature description (#PR-number)

### Changed
- Modified behavior description (#PR-number)

### Fixed
- Bug fix description (#PR-number)
```

---

## Commit Message Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification.

### Format

```
<type>(<scope>): <description>
```

### Types

| Type | Description |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code refactoring |
| `test` | Adding or updating tests |
| `build` | Build system or dependencies |
| `ci` | CI/CD configuration |
| `chore` | Maintenance tasks |

### Scope (Optional)

Common scopes for PyHako:
- `auth` - Authentication
- `sync` - Synchronization
- `client` - API client
- `media` - Media handling
- `blog` - Blog scraping

### Examples

```bash
feat(auth): add TokenManager for secure credential storage
fix(sync): handle session expiration gracefully
docs: update API reference
test(client): add integration tests for refresh flow
```

---

## Testing

### Unit Tests (CI Default)

Unit tests run automatically in CI:
```bash
uv run pytest tests/
```

### Integration Tests (Local Only)

Integration tests require stored credentials. They test real browser refresh and API calls.

**Prerequisites**:

1. Use `BrowserAuth` to log in and store credentials:
   ```python
   import asyncio
   from pyhako import BrowserAuth, Group
   from pyhako.credentials import get_token_manager

   async def login():
       creds = await BrowserAuth.login(Group.HINATAZAKA46)
       tm = get_token_manager()
       tm.save_session("hinatazaka46", creds["access_token"], cookies=creds.get("cookies"))

   asyncio.run(login())
   ```

2. A browser window will open -- complete the login process

3. Credentials are stored in your system keyring

**Run integration tests**:
```bash
uv run pytest tests/test_integration.py -m integration -v
```

### Property-Based Testing

We use Hypothesis for property-based testing:
```bash
uv run pytest tests/ --hypothesis-show-statistics
```

---

## Pull Request Process

### Before Creating a PR

1. **Update your branch** with latest `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout your-branch
   git rebase dev
   ```

2. **Run all checks**:
   ```bash
   uv run pytest -v
   uv run mypy .
   uv run ruff check .
   uv run ruff format --check .
   ```

3. **Update CHANGELOG.md** under `[Unreleased]`

### PR Requirements

- [ ] Branch is up to date with `dev`
- [ ] All tests pass
- [ ] Type checking passes (`mypy`)
- [ ] Linting passes (`ruff`)
- [ ] CHANGELOG.md is updated
- [ ] New code has appropriate test coverage

---

## Code Quality Standards

- **Formatter**: `ruff format`
- **Linter**: `ruff check`
- **Type Hints**: Required for all public functions
- **Type Checker**: `mypy` must pass
- **Docstrings**: Required for public modules, classes, and functions
- **Tests**: Property-based testing encouraged (Hypothesis)

---

## License

By contributing, you agree that your contributions will be licensed under its MIT License.
