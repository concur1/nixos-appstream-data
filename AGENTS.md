# Agent instructions

## Tooling

- Use `uv` for everything Python: dependencies, virtualenvs, and running code and tests. Never call bare `python`/`pip`.
- Lint and format with `ruff`; type-check with `ty`.
- Prefer Polars over Pandas for dataframes.

## Commands

- Add a dependency: `uv add <pkg>` (a dev dependency: `uv add --dev <pkg>`)
- Run a script: `uv run scripts/<name>.py`
- The check suite, to run and make pass at the end of **every** task:
  `uv run ruff format . && uv run ruff check . && uv run ty check && uv run pytest`

## Working rules

- Prefer pure functions that can be easily tested.
- Test behaviour, not implementation.
- Add specific type hints.
- Validate JSON where it is read: catch malformed or truncated JSON before using it.
