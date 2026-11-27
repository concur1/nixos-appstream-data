# Agent instructions

## Tooling

- Use `uv` for everything Python: dependencies, virtualenvs, and running code/tests. Never call bare `python`/`pip`.
- Lint and format with `ruff`.
- Type-check with `ty`.

## Commands

### Dependencies
- Add a dependency: `uv add <pkg>`
- Add a dev dependency: `uv add --dev <pkg>`

### Run code
- Run a script/module: `uv run python -m <module>`
- Run a file: `uv run main.py`

### Check suite
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Type-check: `uv run ty check`
- Test: `uv run pytest`

## Working rules

### General
- At the end of **every** task, run the full check suite and make it pass:
  `uv run ruff format . && uv run ruff check . && uv run ty check && uv run pytest`
- Prefer pure functions that can be easily tested.

### Testing
- Test behaviour not implementation.
- Always add specific type hints/strong typing.

### Tooling
- Prefer Polars over Pandas when using dataframes.
- Prefer Lazyframes over DataFrames when using Polars.
- Use Pydantic in **strict mode** for all validation (`model_config = ConfigDict(strict=True, extra="forbid")`) so types are never silently coerced and unknown fields are rejected.

### Data Pipeline Logic
- Validate JSON at the decode boundary: guard the raw parse (catch malformed/truncated JSON) before Pydantic sees it, then use Pydantic per record. Do deduplication and gap-finding in Polars, not Pydantic. 
- When building ingestion data pipelines include "metadata" about the ingested record (which should be a struct), this should include the ingestion time, the source file/api, pipeline version (optional).
- Output schema: one source of truth. Columns and order come from `Model.model_fields`; one `{field_name: pl.DataType}` map gives the Polars types (metadata columns included).
- Once a data pipeline has been created, add the command to run it to the makefile.

### Reconciliaiton

Verify that a pipeline is working as expected by creating a reconciliaiton notebook in the notebooks/ folder.
Reconciliation notebooks should:
- Run the pipeline
- Run checks such as:
    - A before/after comparison
    - Check for duplicates
    - Check for nulls
    - Count the rows
    - Check for gaps in the input data
