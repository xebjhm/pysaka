# Release Guide for pysaka

This guide describes the automated release process for publishing `pysaka` to PyPI.

## Overview

pysaka uses **tag-triggered CI** with **OIDC trusted publishing** -- no API tokens or manual uploads required.

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

### 2. Roll the Changelog

The `github-release` job (see below) extracts release notes from the matching
`## [X.Y.Z]` section of `CHANGELOG.md`. Before tagging, roll `[Unreleased]` into
a dated version section, or the auto-created Release ships with generic fallback
notes:

1. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and add a fresh empty
   `## [Unreleased]` above it.
2. Update the link block at the bottom: repoint `[Unreleased]` to
   `vX.Y.Z...HEAD` and add `[X.Y.Z]: .../compare/v<prev>...vX.Y.Z`.
3. Flag any `**Breaking:**` change prominently (it may warrant a minor/major
   bump per SemVer).

### 3. Test with TestPyPI (Optional)

Trigger the test publish workflow manually via GitHub Actions to validate the build:

```bash
# Via GitHub CLI
gh workflow run test-publish.yml
```

This builds the package with a dev suffix and publishes to [TestPyPI](https://test.pypi.org/p/pysaka). The workflow also verifies installation on Python 3.9 and 3.12.

### 4. Create and Push a Version Tag

```bash
git tag v0.2.0
git push origin v0.2.0
```

This triggers the `publish.yml` workflow, which:

1. **Verifies** the tag version matches `pyproject.toml`
2. **Builds** the package with `uv build`
3. **Publishes** to PyPI via trusted publishing (OIDC, no API token needed)
4. **Creates a GitHub Release** (`github-release` job) titled `pysaka X.Y.Z`,
   with notes extracted from the `## [X.Y.Z]` section of `CHANGELOG.md`. If that
   section is missing (i.e. you skipped step 2), it falls back to generic notes —
   so rolling the changelog first is what makes the Release readable.

Alternatively, create a **GitHub Release** from the tag manually, which also
triggers the publish workflow.

### 5. Verify

Visit [https://pypi.org/project/pysaka/](https://pypi.org/project/pysaka/) to confirm the release is live.

```bash
uv add pysaka==0.2.0
```

## Prerequisites

- **Trusted Publisher** configured on PyPI for the `publish.yml` workflow
- **Trusted Publisher** configured on TestPyPI for the `test-publish.yml` workflow
- GitHub repository environments `pypi` and `testpypi` set up with `id-token: write` permission
