# Release Guide for PyHako

This guide walks you through the steps to upload `PyHako` to PyPI.

## Prerequisites
- You have a PyPI account: [https://pypi.org/](https://pypi.org/)
- You have an API Token from PyPI: [https://pypi.org/manage/account/token/](https://pypi.org/manage/account/token/)

## 1. Build the Package
We have already verified the build, but to be sure, run:

```bash
uv build
```

This will create a `dist/` directory containing:
- `pyhako-0.1.0-py3-none-any.whl`
- `pyhako-0.1.0.tar.gz`

## 2. Upload to PyPI
Use `twine` to upload the artifacts.

```bash
uv publish
```

## 3. Authentication
When prompted:
- **Username**: `__token__`
- **Password**: Your PyPI API Token (starts with `pypi-`)

## 4. Verify
Visit [https://pypi.org/project/pyhako/](https://pypi.org/project/pyhako/) (once uploaded) to see your package live.
