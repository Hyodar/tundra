# Testing recipes and fragments

`tundravm.testing` checks recipes and fragments without a build backend. Its helpers take the values the lifecycle functions return (`lint()` diagnostics, a compiled `Tree`), so a test reads like the code it tests. Importing it does not import pytest.

```python
from tundravm import compile, lint
from tundravm.testing import assert_clean, assert_diagnostic, assert_tree, compile_tree, fake_bake
```

## Diagnostics

```python
assert_clean(diagnostics, /, *, variants=None, allow=(), strict=None) -> diagnostics
assert_diagnostic(diagnostics, code, /, *, level=None, variant=None, subject=None, variants=None) -> Diagnostic
```

- `assert_clean(lint(recipe))` fails listing every error and warning whose code is not in `allow`. For a list of diagnostics `strict` defaults to true (warnings fail); pass `strict=False` to fail on errors only.
- `assert_diagnostic(lint(recipe), "disk-key-undefined", variant="default")` returns the first diagnostic matching every field you give, or fails listing what was found.
- Both also accept a `Recipe` and lint it for you (then `strict` defaults to false, and `variants=` limits the variants linted).

```python
from tundravm import Disk, Fragment, Key, Recipe, lint
from tundravm.testing import assert_diagnostic


def test_disk_without_its_key():
    disk = Disk("data", mount="/data", key=Key("data"))
    recipe = Recipe(name="t", common=Fragment("t", items=(disk,)))
    found = assert_diagnostic(lint(recipe), "disk-key-undefined", variant="default", subject="data")
    assert found.level == "error"
```

## Compiled trees

```python
compile_tree(recipe, *, variants=None, path=None) -> CompiledTree
```

Compiles the recipe (every variant, or the names in `variants`) into `path` or a fresh temporary directory, without a lockfile. `CompiledTree` reads what tests usually inspect; every `profile` argument is a variant name and defaults to the first compiled variant:

| Method | Returns |
|---|---|
| `tree.root`, `tree.variants` | The directory, the compiled variant names |
| `tree.profile(name)` | The variant's directory |
| `tree.files(name)` | Every file path under it, sorted |
| `tree.read(relpath, name)`, `tree.exists(relpath, name)`, `tree.path(relpath, name)` | One file |
| `tree.conf(name)` | `mkosi.conf` |
| `tree.unit(unit_name, name)` | A shipped unit file (`.service` is optional) |
| `tree.script(phase, name)` | The phase script, e.g. `tree.script("postinst")` |
| `tree.runtime_init(name)` | `/usr/bin/runtime-init` |

```python
from tundravm import Fragment, Recipe, Service
from tundravm.testing import compile_tree


def test_app_unit_is_enabled():
    recipe = Recipe(name="t", common=Fragment("t", items=(Service("app", "/usr/bin/app"),)))
    tree = compile_tree(recipe)
    assert "ExecStart=/usr/bin/app" in tree.unit("app.service")
    assert "systemctl enable app.service" in tree.script("postinst")
```

## Golden trees

```python
assert_tree(tree: Tree, golden: str | Path, *, update=None) -> None
```

Fails unless `golden` holds exactly `tree`: every file path, its bytes, its executable bit and every symlink target. Empty directories are ignored, because git does not keep them. With `update=True`, or `TUNDRAVM_UPDATE_GOLDEN=1` in the environment, it writes `tree` to `golden` instead.

```python
from pathlib import Path

from tundravm import compile, read_lock


def test_committed_tree_is_current():
    locked = read_lock(Path("build/tundravm.lock"))
    assert_tree(compile(recipe, lock=locked), Path("mkosi"))
```

`compile(recipe)` without `lock=` ignores any lockfile and builds sources from their refs, while `tundravm compile` applies `build/tundravm.lock` when it exists. Compare like with like: pass the lock when the committed tree was compiled with one.

```bash
TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py   # accept the new tree, then review the git diff
```

## Simulated artifacts

```python
fake_bake(tree: Tree, *, variant: str, target: Target, out: str | Path) -> Artifact
```

Writes a simulated disk file for one variant under `out` and records it in `out/bake-result.json` (merging with one already there), so `read_artifacts()`, `tundravm measure` and `tundravm deploy` find it. The artifact is `simulated`: `measure` and `deploy` refuse it unless `allow_placeholder=True`.

```python
from tundravm import compile
from tundravm.declarative import measure
from tundravm.testing import fake_bake


def test_measure_placeholder(recipe, tmp_path):  # `recipe` is the plugin's minimal fixture
    artifact = fake_bake(compile(recipe), variant="default", target="qemu", out=tmp_path)
    assert measure(artifact, allow_placeholder=True).tool == "placeholder"
```

For a full bake without tools, `bake_in_process(recipe, out=tmp_path)` runs the real pipeline (lint, lock check, compile) on the in-process backend and returns simulated artifacts. Without `locked=` it locks offline first, which fails for a recipe with source builds: pass a `Lock` (for example `lock(recipe, resolver=...)`) for those.

## Composition fakes

```python
fake_fragment(name="fake", *, packages=(), files=None, init=None, priority=50, requires=(), checks=()) -> Fragment
```

A small fragment for testing how fragments compose: a `Package` per name, a `File` per `{path: content}`, an `Init` step when `init` is given, and the `requires`/`checks` you pass.

## CLI tests

```python
recipe_file(tmp_path, source, name="recipe.py") -> Path
run_cli(*argv) -> tuple[int, str, str]
```

`recipe_file` writes dedented source; `run_cli` runs `tundravm` in-process and returns the exit code, stdout and stderr.

```python
from tundravm.testing import recipe_file, run_cli


def test_lint_fails_on_a_dangling_key(tmp_path):
    path = recipe_file(tmp_path, """
        from tundravm import Disk, Fragment, Key, Recipe
        recipe = Recipe(name="t", common=Fragment("t", items=(Disk("data", mount="/data", key=Key("data")),)))
    """)
    code, out, err = run_cli("lint", path)
    assert code == 1
    assert "disk-key-undefined" in out
```

## Pytest fixtures

The `tundravm` pytest plugin is registered through an entry point, so installing tundravm makes its fixtures available (disable with `pytest -p no:tundravm`):

| Fixture | Gives |
|---|---|
| `recipe` | A minimal `Recipe(name="test", common=Fragment("test"))` with one `default` variant |
| `compiled` | `compiled(recipe, variants=None) -> CompiledTree`, compiled into a fresh directory under `tmp_path` |
| `run_cli` | The `run_cli` helper |

A test module that defines its own `recipe` fixture overrides the plugin's.

## Running the repository's tests

```bash
uv run pytest                                  # everything
uv run pytest tests/test_declarative_utils.py    # the surge recipe against its committed tree
```
