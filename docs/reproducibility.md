# Reproducibility Validation

Use the same workflow locally and in CI:

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```

The repository includes reproducibility-focused tests that run two equivalent bake flows and assert artifact digest stability.

## mkosi v26 Requirements

The SDK requires mkosi >= 25 (v26 recommended). The `local_linux` backend
checks the installed version at prepare time and raises `E_BACKEND_EXECUTION`
if the version is too old.

Install mkosi v26:

```bash
pip install --break-system-packages 'mkosi @ git+https://github.com/systemd/mkosi.git@v26'
```

Reproducibility settings emitted in `mkosi.conf`:
- `SourceDateEpoch=0` and `Environment=SOURCE_DATE_EPOCH=0`
- `Seed=<stable-uuid>` for deterministic partition UUIDs
- `CompressOutput=zstd`
- `ManifestFormat=json` for reproducible manifest output
- `CleanPackageMetadata=yes` to strip volatile package metadata

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
