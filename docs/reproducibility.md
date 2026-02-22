# Reproducibility Validation

Use the same workflow locally and in CI:

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```

The repository includes reproducibility-focused tests that run two equivalent bake flows and assert artifact digest stability.

## CI Mode

For strict CI reproducibility, combine frozen bakes and strict policy:

```python
from tdx.policy import Policy

policy = Policy(
    require_frozen_lock=True,
    mutable_ref_policy="error",
    require_integrity=True,
)
```

Then run bake with frozen lock enforcement:

```python
img.set_policy(policy)
img.lock()
img.bake(frozen=True)
```
