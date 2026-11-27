# Contributing

## Local setup

Attestory supports Python 3.11 and newer. Create and activate a virtual
environment, then install the development and HTTP bridge dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Checks

Run the test suite and Ruff before opening a pull request:

```bash
pytest -v
ruff check .
```

Keep changes focused, add tests for new behavior, and avoid committing local
`.attestory/` workspaces or generated snapshots.
