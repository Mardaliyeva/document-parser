# Contributing

Thank you for helping improve `document-parser`.

## Development environment

Use Python 3.11 or 3.12. Create an isolated environment and install the project
with the development dependencies:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

Equivalent `python3` and virtual-environment commands may be used on Linux or
macOS.

## Before opening a pull request

Run all local checks:

```powershell
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy src tests
.venv\Scripts\python -m pytest
.venv\Scripts\python -m build
.venv\Scripts\python -m twine check dist\*
```

Pull requests should include tests for changed behavior and update public
documentation when interfaces change. Do not combine unrelated changes in one
pull request.

## Dependencies and models

Read [the dependency policy](docs/dependency-policy.md) before adding a package or
model. A dependency proposal must record its exact purpose, source, license, and
the alternatives considered.

Do not commit downloaded model weights, conversion outputs, confidential source
documents, credentials, or local caches. Public tests must use synthetic or
explicitly redistributable fixtures.

## Code conventions

- Keep the package typed and pass strict mypy checks.
- Use Ruff for formatting and linting.
- Keep format-specific behavior behind an adapter interface.
- Preserve source facts; never introduce silent semantic rewriting.
- Avoid application-specific storage, indexing, authentication, or UI code.

## Security issues

Do not open a public issue for a suspected vulnerability. Follow
[SECURITY.md](SECURITY.md).

By submitting a contribution, you agree that it is licensed under the Apache
License, Version 2.0.

