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
from tundravm.policy import Policy

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

## Lockfile sections

Lockfile schema v2 adds `sections`: a SHA-256 per recipe section (`base`, `arch`, `default_profile`, `init_scripts`, `profiles.<name>.<section>`), computed over the same canonical JSON as `recipe_digest`. The whole-recipe digest is unchanged and is still what frozen bakes enforce. The sections only explain a mismatch.

```python
drift = img.lock_status()      # reads <build_dir>/tundravm.lock, never writes
drift.is_clean                 # False when anything changed
drift.changed, drift.added, drift.removed
print(drift.render())          # "~ profiles.default.packages: +htop" or "lock is up to date"
```

`lock_status(path=None)` returns a `LockDrift` and raises `LockfileError` when the lockfile is missing or unreadable. Item detail comes from the recipe payload embedded in the lockfile, and only when it still matches the recorded section digest. From the shell: `tundravm lock RECIPE --check`.

Lockfiles written before v2 still pass frozen bakes, but `lock --check` reports every section as `+` until you re-lock.

## Pinned sources

Modules that build from source declare it instead of writing bash:

```python
from tundravm import GitSource, GoBuild, SourceBuild

img.source_build(SourceBuild(
    name="tdxs",
    source=GitSource("https://github.com/Hyodar/tundra-tools.git", "master"),
    build=GoBuild(package="./cmd/tdxs", output="tdxs"),
    install_to="/usr/bin/tdxs",
))
```

The recipe digest records the symbolic declaration (`master`), never the resolved commit, so locking does not make its own lockfile stale. `tundravm lock` resolves every git ref to a commit and every `HttpSource` without `sha256` to a hash, and stores them in the lockfile's `fetches` (with `name` and `ref`). `compile()` reads `<build_dir>/tundravm.lock` and, where a pin exists for the same repo and ref, fetches that exact commit instead of the branch.

- `tundravm lock --offline` (or `policy.network_mode="offline"`) reuses existing pins and fails naming any source that would need the network.
- `mutable_ref_policy`: `"warn"` (default) leaves `compile()` silent and relies on `check` (`source-unpinned`), `explain` (`pinned=-`) and frozen bakes; `"error"` makes `compile()` fail on an unpinned source; `"allow"` downgrades the check to info. A lockfile pin satisfies every policy.
- `bake --frozen` refuses unpinned sources with the names to pin.
- `lock --check` shows `+ sources.<name>`, `- sources.<name>` and `~ sources.<name>: <old7> -> <new7>`.
- `SourceBuild(mark_unpinned=False)` keeps the build hook free of an `# unpinned:` comment (the built-in modules use it to keep existing trees byte-identical); `cache_key=` overrides the build-cache key.
