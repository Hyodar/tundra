# Testing recipes and modules

`tundravm.testing` packages the helpers the SDK's own test suite uses, so a recipe or a third-party module can be tested in a few lines, without mkosi, a VM, or root. Everything runs in-process: compile to a temp dir and read files back, lint with `check()`, compare against a committed golden tree, bake with placeholder artifacts, and drive the CLI.

## Quick start

Installing tundravm registers a pytest plugin (the `pytest11` entry point `tundravm`), so its fixtures are available in every test with no `conftest.py` changes. Turn it off with `pytest -p no:tundravm`.

```python
from tundravm import Image
from tundravm.testing import assert_clean


def test_recipe(image: Image, compiled) -> None:
    image.install("curl")
    image.service("app", command="/usr/bin/app")
    assert_clean(image)
    assert "curl" in compiled(image).conf()
```

| Fixture | Provides |
| --- | --- |
| `image` | A fresh `Image(reproducible=True)` with no backend |
| `inprocess_image` | An `Image` with `InProcessBackend()` and `build_dir=tmp_path / "build"`, so `bake()` works anywhere |
| `compiled` | Factory `compiled(image, profiles=None) -> CompiledTree`; each call compiles into a new dir under `tmp_path` |
| `run_cli` | `run_cli(*argv) -> (exit_code, stdout, stderr)` |

For type hints, `tundravm.testing.pytest_plugin` exports `CompileFactory` (the `compiled` fixture) and `CliRunner` (the `run_cli` fixture). A fixture of the same name in your own `conftest.py` takes precedence over the plugin's.

## Helpers

All helpers live in `tundravm.testing`. Importing it does not import pytest; failures are plain `AssertionError`s, so the helpers work under any test runner. Every `profiles=` argument takes a list of profile names (or a single name) and defaults to the image's active profiles.

### `compile_tree`

`compile_tree(image, *, profiles=None, path=None) -> CompiledTree` compiles into `path`, or a fresh temp dir when `path` is `None`. The image's compile cache is left untouched, so a later `bake()` behaves as if `compile_tree` never ran. Unknown profile names raise `ValueError` instead of silently compiling an empty profile.

```python
tree = compile_tree(img, profiles=["default", "azure"])
assert "curl" in tree.conf()
assert "User=app" in tree.unit("app")                 # app.service
assert "useradd" in tree.script("postinst")           # scripts/NN-postinst.sh
assert tree.read("mkosi.extra/etc/motd") == "hello\n"
assert "jq" in tree.conf(profile="azure")
```

`CompiledTree` methods. `profile=` defaults to the image's default profile, or the first compiled one when that profile was not compiled.

| Method | Returns |
| --- | --- |
| `root`, `profiles` | Tree root `Path`; tuple of compiled profile names |
| `profile(name)` | The profile's directory; `KeyError` if it was not compiled |
| `path(relpath)`, `read(relpath)`, `exists(relpath)` | Path, text, or existence of a file relative to the profile directory |
| `files()` | Sorted POSIX paths of every file in the profile |
| `unit(name)` | `mkosi.extra/usr/lib/systemd/system/<name>`; a bare name gets `.service` |
| `script(phase)` | `scripts/NN-<phase>.sh` |
| `runtime_init()` | `mkosi.extra/usr/bin/runtime-init` |
| `conf()` | `mkosi.conf` |

`CompiledTree` implements `__fspath__`, so it can be passed to `open()`, `Path()`, or `diff_trees()`. A missing unit or script raises `FileNotFoundError` naming the ones that exist.

### `assert_clean`

`assert_clean(image, *, profiles=None, allow=(), strict=False) -> list[Diagnostic]` runs `image.check()` and fails on any error-level finding whose code is not in `allow`. With `strict=True`, warnings and infos fail too. The failure message is the same report `tundravm check` prints. On success it returns every diagnostic, allowed or not.

```python
assert_clean(img)
assert_clean(img, strict=True, allow=["backend-missing"])
assert_clean(img, profiles=["azure"])
```

### `assert_diagnostic`

`assert_diagnostic(image, code, *, level=None, profile=None, subject=None) -> Diagnostic` returns the first finding that matches every given field. If none matches, it fails and lists every finding. Pass `profile=` to lint a profile that is not active.

```python
img.service("app", command="/usr/bin/app", user="ghost")
d = assert_diagnostic(img, "service-user-missing", level="error", subject="app")
assert "ghost" in d.message
```

### `assert_tree_matches`

`assert_tree_matches(image, golden_dir, *, profiles=None, update=None) -> TreeDiff` compiles into a temp dir and diffs it against `golden_dir` with `tundravm.diff.diff_trees`. On a mismatch it fails with `TreeDiff.stat()` and the first 200 lines of the unified diff. Profile directories in `golden_dir` that were not compiled are neither compared nor modified. See [Golden trees](#golden-trees).

```python
GOLDEN = Path(__file__).parent / "golden" / "agent"

def test_agent_tree(image: Image) -> None:
    image.apply(Agent())
    assert_tree_matches(image, GOLDEN)
```

### `bake_in_process`

`bake_in_process(image, *, build_dir=None, profiles=None) -> BakeResult` swaps in `InProcessBackend()`, bakes into `build_dir` (or a temp dir), and restores the original backend, even if the bake raises. Artifacts are small deterministic placeholder files, so this tests the bake pipeline (lint gate, compile, artifact layout) rather than mkosi itself.

```python
result = bake_in_process(img, build_dir=tmp_path / "build")
assert result.profiles["default"].artifacts["qemu"].path.is_file()
assert img.backend is None  # restored
```

### `FakeModule`

`FakeModule(name="fake", *, packages=(), files=None, init_script=None, init_priority=50, requires=())` is a ready-made `Module` for testing how your module composes with others. `install()` declares the packages and files, `init_script()` returns `init_script`, and `applied_to` records every profile it was applied to. Each instance gets its own subclass, so `requires` accepts fake instances as well as module classes.

```python
a = FakeModule("a", init_script="echo a", init_priority=10)
b = FakeModule("b", packages=("curl",), files={"/etc/b": "b\n"}, requires=(a,))
img.apply(a, b)                  # img.apply(b) alone raises ValidationError
assert b.applied_to == ["default"]
assert img.applied_modules() == (a, b)
```

Two fakes with the same `init_priority` trigger the `init-priority-collision` warning. Give them different priorities unless you are testing that rule.

## Golden trees

A golden test commits the compiled tree next to the test and fails when the recipe's output changes. Reviewers see the effect of a recipe or module change as a file diff.

1. Write the test with `assert_tree_matches(image, GOLDEN)`.
2. Create or refresh the golden tree: `TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests/test_agent.py`. With the variable set (and `update` left as `None`), the helper replaces the compiled profiles in `golden_dir` instead of failing. Files that are no longer emitted are deleted. Other profiles' directories are kept.
3. Review the change with `git diff tests/golden/` and commit it with the code change.
4. Without the variable, any drift fails with the stat and diff:

```
compiled tree differs from tests/golden/agent
M  default/mkosi.conf
A  default/mkosi.extra/etc/new
2 files changed
...
Re-run with TUNDRAVM_UPDATE_GOLDEN=1 to accept the compiled tree.
```

Pass `update=True` or `update=False` to override the environment. Golden trees are deterministic only for `reproducible=True` images (the default, and what the `image` fixture uses).

## CLI testing

`recipe_file(tmp_path, source, name="recipe.py") -> Path` writes a recipe file, dedenting `source` so it can be an indented triple-quoted string, and creates parent directories. `run_cli(*argv) -> (exit_code, stdout, stderr)` runs `tundravm.cli.main` in-process. It captures both streams, accepts `Path` arguments, and turns argparse's `SystemExit` into an exit code (2 for usage errors).

```python
def test_cli_check(tmp_path: Path, run_cli) -> None:
    path = recipe_file(tmp_path, """
        from tundravm import Image
        img = Image()
        img.install("curl")
    """)
    code, out, err = run_cli("check", path, "--strict")
    assert code == 1 and "backend-missing" in out and err == ""
```

SDK errors are printed to stderr as `error [<code>]: ...`, with the exit codes listed in [`cli.md`](cli.md).
