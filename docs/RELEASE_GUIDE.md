# Release Guide for PyHako

This guide describes the automated release process for publishing `PyHako` to PyPI.

## Overview

PyHako uses **tag-triggered CI** with **OIDC trusted publishing** -- no API tokens or manual uploads required.

### Workflow Files

| Workflow | File | Trigger |
|----------|------|---------|
| Tests | `.github/workflows/test.yml` | Push/PR to `main`, `dev` |
| Publish to PyPI | `.github/workflows/publish.yml` | Version tags (`v*.*.*`) or GitHub Release |
| Test Publish to TestPyPI | `.github/workflows/test-publish.yml` | Manual dispatch or PR to `main` |

## Release Process

### 1. Update Version

Update the version in `pyproject.toml`:

```toml
version = "0.2.0"
```

### 2. Test with TestPyPI (Optional)

Trigger the test publish workflow manually via GitHub Actions to validate the build:

```bash
# Via GitHub CLI
gh workflow run test-publish.yml
```

This builds the package with a dev suffix and publishes to [TestPyPI](https://test.pypi.org/p/pyhako). The workflow also verifies installation on Python 3.9 and 3.12.

### 3. Create and Push a Version Tag

```bash
git tag v0.2.0
git push origin v0.2.0
```

This triggers the `publish.yml` workflow, which:

1. **Verifies** the tag version matches `pyproject.toml`
2. **Builds** the package with `uv build`
3. **Publishes** to PyPI via trusted publishing (OIDC, no API token needed)

Alternatively, create a **GitHub Release** from the tag, which also triggers the publish workflow.

### 4. Verify

Visit [https://pypi.org/project/pyhako/](https://pypi.org/project/pyhako/) to confirm the release is live.

```bash
uv add pyhako==0.2.0
```

## Prerequisites

- **Trusted Publisher** configured on PyPI for the `publish.yml` workflow
- **Trusted Publisher** configured on TestPyPI for the `test-publish.yml` workflow
- GitHub repository environments `pypi` and `testpypi` set up with `id-token: write` permission
